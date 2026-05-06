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

MatchedVia = Literal["exact", "case", "diacritic", "token_set", "prefix", "fuzzy"]


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


# -- person-matching diacritic fold (more aggressive) ----------------------


# Mojibake substitutions observed in pre-2018 MO PostScript-conversion
# artefacts. Each mapping replaces a single mojibake character with the
# ASCII-folded form of the Romanian character it represents.
#
# Scope is intentionally narrow: only chars that are NOT legitimate in
# Romanian text. We do NOT include `ã`, `â` here — those NFKD-strip to
# `a` correctly anyway, and `â` is a legitimate Romanian letter.
#
# This is used by `_strip_diacritics_aggressive`, which feeds the
# person-matcher; the existing ministry / institutional matchers stay on
# the strict `_strip_diacritics` to avoid false-positive collisions.
_PERSON_MOJIBAKE_MAP = str.maketrans(
    {
        "„": "a",  # double low-9 quotation mark — mojibake for `ă`
        "∫": "s",  # integral — mojibake for `ș`
        "˛": "t",  # combining ogonek (free-standing) — mojibake for `ț`
        "º": "s",  # masculine ordinal — mojibake for `ș`
        "ª": "S",  # feminine ordinal — mojibake for `Ș`
        "þ": "t",  # thorn — mojibake for `ț` (cedilla-era)
        "Þ": "T",  # Thorn — mojibake for `Ț`
        "™": "S",  # trade mark — mojibake for `Ș` (e.g. `™edinþa`)
        "Ð": "I",  # eth — mojibake for `Î`
        "ð": "i",  # eth — mojibake for `î`
    }
)


def _strip_diacritics_aggressive(s: str) -> str:
    """Diacritic strip + mojibake fold — used only for person matching.

    Person names land in the corpus as a soup of (a) modern comma forms,
    (b) cedilla forms, (c) PostScript-conversion mojibake (`V„c„roiu` for
    `Văcăroiu`, `Mele∫canu` for `Meleșcanu`, `Bolca∫` for `Bolcaș`). The
    aggressive fold normalises all three into ASCII so the matcher's
    `diacritic` tier can collide them. The strict `_strip_diacritics`
    used by the ministry / institutional matchers is unchanged.
    """
    s = s.translate(_PERSON_MOJIBAKE_MAP)
    return _strip_diacritics(s)


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


# -- persons (4.3) ----------------------------------------------------------


@lru_cache(maxsize=1)
def _load_persons() -> dict:
    path = _REGISTRIES_DIR / "persons.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _validate_person_entries(data.get("entries") or [], registry_name="persons")
    return data


def _validate_person_entries(entries: list[dict], *, registry_name: str) -> None:
    """Person registry validation. Stricter than the institutional one
    because mandate dates and homonym disambiguation must be well-shaped.
    """
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
        mandates = entry.get("mandates") or []
        if not isinstance(mandates, list):
            raise ValueError(
                f"{registry_name}: entry {entry['id']!r} has malformed mandates"
            )
        for m in mandates:
            if not isinstance(m, dict):
                raise ValueError(
                    f"{registry_name}: entry {entry['id']!r} mandate is not an object"
                )
            for k in ("from", "to"):
                v = m.get(k)
                if v is not None and not (
                    isinstance(v, str) and len(v) >= 4 and v[:4].isdigit()
                ):
                    raise ValueError(
                        f"{registry_name}: entry {entry['id']!r} mandate {k!r}={v!r} "
                        "must be ISO date prefix or null"
                    )


def load_persons() -> dict:
    """Public accessor — returns the parsed persons.json dict."""
    return _load_persons()


PERSONS_REGISTRY_VERSION: str = _load_persons()["version"]


# Honorifics + parliamentary-title prefixes that callers commonly leave
# attached to the speaker raw. We strip these once per call before
# matching — a quick state-machine peel rather than a lookahead regex
# because the token list is short and stable.
_PERSON_HONORIFICS = (
    "domnișoara",
    "domnisoara",
    "domnişoara",
    "domnul",
    "doamna",
    "dl.",
    "dna.",
    "dl",
    "dna",
)
_PERSON_TITLES = (
    "deputat",
    "deputata",
    "deputatã",
    "deputatul",
    "senator",
    "senatorul",
    "senatoare",
    "ministru",
    "ministrul",
    "ministrului",
    "secretar",
    "secretarul",
    "președinte",
    "presedinte",
    "preşedinte",
    "președintele",
    "presedintele",
    "preşedintele",
    "vicepreședinte",
    "vicepresedinte",
    "vicepreşedinte",
    "viceprim-ministru",
    "viceprim",
    "prim-ministru",
    "premier",
    "europarlamentar",
)


def _peel_person_prefix(s: str) -> str:
    """Peel a leading `Domnul deputat` / `Doamna ministru` etc. from a
    raw speaker string. Iterates token-by-token at most twice (one
    honorific + one title); never consumes name tokens.
    """
    s = s.strip()
    s_lower = s.lower()
    for hon in _PERSON_HONORIFICS:
        if s_lower.startswith(hon + " ") or s_lower == hon:
            s = s[len(hon) :].lstrip()
            s_lower = s.lower()
            break
    for tt in _PERSON_TITLES:
        if s_lower.startswith(tt + " ") or s_lower == tt:
            s = s[len(tt) :].lstrip()
            s_lower = s.lower()
            break
    return s


def _peel_role_suffix(s: str) -> str:
    """Drop a trailing role clause after the first comma.

    Speaker raws like `Domnul deputat NAME, vicepreședintele Camerei`
    place the name before the comma; we keep that span only. Same applies
    to interpellation questioner / committee signatures.
    """
    if "," in s:
        return s.split(",", 1)[0].strip()
    return s.strip()


def _normalize_for_match(s: str) -> str:
    """Full preprocessing pipeline applied before each match tier.

    - peel honorific + title prefix
    - peel role suffix
    - collapse internal whitespace (newlines from sloppy MD wrapping)
    - strip trailing punctuation (`.`, `:`)
    """
    s = _peel_role_suffix(_peel_person_prefix(s))
    s = re.sub(r"\s+", " ", s).strip()
    return s.rstrip(".:").strip()


@lru_cache(maxsize=1)
def _persons_alias_index() -> tuple[
    dict[str, str],  # exact
    dict[str, str],  # casefold
    dict[str, str],  # diacritic+mojibake-folded + casefold
    list[tuple[frozenset[str], str]],  # token-set
    dict[str, list[str]],  # diacritic-folded → list of person_ids (homonym index)
    dict[str, dict],  # person_id → entry (fast lookup for context_year scoring)
]:
    data = _load_persons()
    exact: dict[str, str] = {}
    case: dict[str, str] = {}
    diacritic: dict[str, str] = {}
    token: list[tuple[frozenset[str], str]] = []
    homonym: dict[str, list[str]] = {}
    by_id: dict[str, dict] = {}
    for entry in data["entries"]:
        eid = entry["id"]
        by_id[eid] = entry
        names: list[str] = [entry["canonical_name"], *(entry.get("aliases") or [])]
        diacritic_form = entry.get("diacritic_form")
        if isinstance(diacritic_form, str) and diacritic_form:
            names.append(diacritic_form)
        for name in names:
            exact.setdefault(name, eid)
            case.setdefault(name.casefold(), eid)
            d_key = _strip_diacritics_aggressive(name).casefold()
            diacritic.setdefault(d_key, eid)
            homonym.setdefault(d_key, []).append(eid)
            token_set = _person_token_set(name)
            if token_set:
                token.append((token_set, eid))
    # Dedup the homonym lists so duplicated aliases on a single entry
    # don't manufacture fake homonyms.
    for k, v in homonym.items():
        seen: list[str] = []
        for eid in v:
            if eid not in seen:
                seen.append(eid)
        homonym[k] = seen
    return exact, case, diacritic, token, homonym, by_id


_PERSON_TOKEN_SPLIT_RE = re.compile(r"[\s\-]+", re.UNICODE)


def _person_token_set(s: str) -> frozenset[str]:
    """Token-set form for person names — diacritic+mojibake-folded.

    Splits on whitespace AND hyphens (so `Sorin-Mihai` and `Sorin Mihai`
    collapse). Strips punctuation per token. Empty tokens dropped.
    """
    folded = _strip_diacritics_aggressive(s).lower()
    parts = _PERSON_TOKEN_SPLIT_RE.split(folded)
    cleaned = [re.sub(r"[^a-z0-9]+", "", t) for t in parts]
    return frozenset(t for t in cleaned if t and len(t) >= 2)


def _levenshtein(a: str, b: str, *, max_distance: int = 2) -> int:
    """Pure-Python Levenshtein with a hard early-exit at `max_distance + 1`.

    Used only as the last-tier fuzzy fall-through for person matching.
    Returns `max_distance + 1` (i.e. "too far") whenever the distance
    exceeds the cap; the caller treats that as a no-match.

    Time is O(|a| × min(|b|, max_distance + 1)) thanks to the band-only
    scan. Implementation kept terse — full algorithmic clarity isn't
    needed because this is a single-purpose 30-line helper.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > max_distance:
        return max_distance + 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    prev = list(range(la + 1))
    cur = [0] * (la + 1)
    for j in range(1, lb + 1):
        cur[0] = j
        bch = b[j - 1]
        # Band: only the columns that could yield ≤ max_distance.
        lo = max(1, j - max_distance)
        hi = min(la, j + max_distance)
        if lo > 1:
            cur[lo - 1] = max_distance + 1  # sentinel beyond band
        row_min = max_distance + 1
        for i in range(lo, hi + 1):
            cost = 0 if a[i - 1] == bch else 1
            cur[i] = min(
                prev[i] + 1,
                cur[i - 1] + 1,
                prev[i - 1] + cost,
            )
            if cur[i] < row_min:
                row_min = cur[i]
        if row_min > max_distance:
            return max_distance + 1
        prev, cur = cur, prev
    return prev[la]


def _mandate_active_in(entry: dict, year: int | None) -> bool:
    """Return True iff `entry` has at least one mandate that overlaps `year`.

    `year=None` matches every entry (caller hasn't supplied a context to
    discriminate against). A mandate with no `to` is treated as ongoing.
    """
    if year is None:
        return True
    mandates = entry.get("mandates") or []
    if not mandates:
        return True  # No mandate data ⇒ don't penalise; let the caller decide.
    for m in mandates:
        m_from = m.get("from")
        m_to = m.get("to")
        from_year = (
            int(m_from[:4])
            if isinstance(m_from, str) and len(m_from) >= 4 and m_from[:4].isdigit()
            else None
        )
        to_year = (
            int(m_to[:4])
            if isinstance(m_to, str) and len(m_to) >= 4 and m_to[:4].isdigit()
            else None
        )
        if from_year is None and to_year is None:
            continue
        # treat missing `to` as "ongoing" relative to year
        if from_year is not None and year < from_year:
            continue
        if to_year is not None and year > to_year:
            continue
        return True
    return False


def _resolve_homonym(
    candidates: list[str], *, context_year: int | None, by_id: dict[str, dict]
) -> str | None:
    """Pick the active-mandate winner from a homonym set.

    When `context_year` is None or no candidate's mandate covers it,
    returns None — the caller treats this as "ambiguous, do not resolve"
    rather than guessing. (We could prefer the lexicographically-first id,
    but that would silently mis-resolve homonyms; better to leave the
    Speaker.person_id null and surface in the long-tail report.)
    """
    if len(candidates) == 1:
        return candidates[0]
    if context_year is None:
        return None
    active = [
        c for c in candidates if _mandate_active_in(by_id.get(c, {}), context_year)
    ]
    if len(active) == 1:
        return active[0]
    return None


def normalize_speaker(
    raw: str | None,
    *,
    context_year: int | None = None,
) -> tuple[str | None, MatchedVia | None]:
    """Map a free-text speaker raw (or pre-cleaned name) to a person_id.

    The matcher peels the leading `Domnul/Doamna [deputat|senator|...]`
    decoration and trailing `, role` clause, then runs the standard tier
    cascade against `persons.json`:

      1. exact         — byte-equal match against canonical_name / alias
                          / diacritic_form (post-peel string).
      2. case          — case-insensitive equality.
      3. diacritic     — diacritic-stripped + mojibake-folded equality.
                          This is what catches `V„c„roiu` ↔ `Văcăroiu`,
                          `Mele∫canu` ↔ `Meleșcanu`, etc.
      4. token_set     — orderless token-set match (diacritic+mojibake-
                          folded). Catches `Iordache Florin` ↔ `Florin
                          Iordache` and `Sorin-Mihai` ↔ `Sorin Mihai`.
      5. fuzzy         — Levenshtein ≤ 2 on the diacritic-folded form.
                          Justified for human names per the design doc;
                          the cap of 2 is tight enough to keep precision
                          high while absorbing OCR slips like a missing
                          dash or a single-letter swap.

    Homonym disambiguation: when a tier resolves to multiple candidate
    person_ids, `context_year` (the MO year) breaks ties by selecting the
    candidate whose mandates cover that year. If no candidate's mandate
    covers it, the matcher returns `(None, None)` rather than guessing.

    `raw=None`, empty input, or institution/procedural labels (`Din sală`,
    `Guvernul`, `<chair narration>`) return `(None, None)` — the caller
    leaves Speaker.person_id null. These non-canonical speakers stay
    visible via Speaker.raw on the sidecar.

    Returns `(id, matched_via)` on hit, `(None, None)` on miss.
    """
    if not isinstance(raw, str):
        return (None, None)
    cleaned = _normalize_for_match(raw)
    if not cleaned or len(cleaned) < 2:
        return (None, None)

    # Filter out structural non-canonical speakers cheaply (these are
    # populated as Speaker dicts by the extractor for procedural turns
    # but should not match a registry person).
    cleaned_low = cleaned.casefold()
    if cleaned_low in _NON_CANONICAL_SPEAKER_LABELS:
        return (None, None)

    exact_idx, case_idx, dia_idx, token_idx, homonym_idx, by_id = _persons_alias_index()

    # All tiers route through `homonym_idx` so an ambiguous match (two
    # entries sharing the same canonical_name / alias surface form) can
    # be disambiguated via context_year — or rejected when no year is
    # given. The diacritic key collapses cedilla, modern, and mojibake
    # surface forms onto one bucket; every alias of every entry is
    # registered there.
    diacritic_key = _strip_diacritics_aggressive(cleaned).casefold()

    def _hit(
        via: MatchedVia, candidates_eid: str | None
    ) -> tuple[str | None, MatchedVia | None]:
        """If the candidate set contains homonyms, run year disambiguation;
        otherwise return the single hit. None on miss."""
        if candidates_eid is None:
            return (None, None)
        candidates = homonym_idx.get(diacritic_key, [candidates_eid])
        if len(candidates) == 1:
            return (candidates[0], via)
        chosen = _resolve_homonym(candidates, context_year=context_year, by_id=by_id)
        if chosen is not None:
            return (chosen, via)
        # Ambiguous + no year disambiguation → bubble through to the next
        # tier; if every tier is ambiguous, the matcher gives up.
        return (None, None)

    eid_or_none, via_or_none = _hit("exact", exact_idx.get(cleaned))
    if eid_or_none is not None:
        return (eid_or_none, via_or_none)
    eid_or_none, via_or_none = _hit("case", case_idx.get(cleaned.casefold()))
    if eid_or_none is not None:
        return (eid_or_none, via_or_none)
    if diacritic_key:
        eid_or_none, via_or_none = _hit("diacritic", dia_idx.get(diacritic_key))
        if eid_or_none is not None:
            return (eid_or_none, via_or_none)

    raw_tokens = _person_token_set(cleaned)
    if len(raw_tokens) >= 2:
        token_candidates: list[str] = []
        for token_set, eid in token_idx:
            if len(token_set) >= 2 and token_set == raw_tokens:
                if eid not in token_candidates:
                    token_candidates.append(eid)
        if token_candidates:
            chosen = _resolve_homonym(
                token_candidates, context_year=context_year, by_id=by_id
            )
            if chosen is not None:
                return (chosen, "token_set")

    # Fuzzy tier: Levenshtein ≤ 2 on the diacritic-folded form, restricted
    # to candidates with at least 2 tokens to avoid a fuzzy match
    # producing a single-token false positive (e.g., `Popa` matching one
    # of many `Popa`-suffixed entries).
    if len(raw_tokens) >= 2:
        best: list[tuple[int, str]] = []
        for token_set, eid in token_idx:
            if len(token_set) < 2:
                continue
            # Compare the joined-token canonical form (sorted) so order
            # doesn't sabotage the distance metric.
            cand_str = " ".join(sorted(token_set))
            input_str = " ".join(sorted(raw_tokens))
            d = _levenshtein(input_str, cand_str, max_distance=2)
            if d <= 2:
                best.append((d, eid))
        if best:
            best.sort()
            top_d = best[0][0]
            top_candidates: list[str] = []
            for d, eid in best:
                if d > top_d:
                    break
                if eid not in top_candidates:
                    top_candidates.append(eid)
            chosen = _resolve_homonym(
                top_candidates, context_year=context_year, by_id=by_id
            )
            if chosen is not None:
                return (chosen, "fuzzy")

    return (None, None)


# Procedural / institutional non-canonical labels surfaced as Speaker
# dicts by the extractor (chair narration markers, audience interjection
# markers, group-attribution markers). These stay `person_id: null` —
# they're not people in the registry sense.
_NON_CANONICAL_SPEAKER_LABELS = frozenset(
    {
        "<chair narration>",
        "din sală",
        "din sala",
        "din sal",  # mojibake remnant
        "guvernul",
        "guvern",
        "guvernul româniei",
        "voci",
        "voci din sală",
        "voci din sala",
        "vocea din sală",
        "aplauze",
    }
)


__all__ = [
    "INSTITUTIONAL_BODIES_REGISTRY_VERSION",
    "MINISTRIES_REGISTRY_VERSION",
    "PERSONS_REGISTRY_VERSION",
    "MatchedVia",
    "load_institutional_bodies",
    "load_ministries",
    "load_persons",
    "normalize_addressee",
    "normalize_institutional_body",
    "normalize_ministry",
    "normalize_speaker",
]
