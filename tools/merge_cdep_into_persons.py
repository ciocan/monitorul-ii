"""Merge `data/seed_cdep.jsonl` (one row per deputy-mandate) into `persons.json`.

# Why a separate merge tool

`scrape_cdep.py` produces one JSONL row per (deputy, legislature) pair —
i.e., one row per *mandate*, not per person. A deputy who served in
legislatures VII, VIII, and IX shows up three times. The merge tool's
job is to:

  1. Group rows by canonical person identity (name-form + party history).
  2. Mint a stable kebab-case `id` for each unique person.
  3. Generate aliases for name-order variants (cdep displays
     `SURNAME Firstname`; the matcher's token-set tier handles ordering
     once both forms are in the alias list).
  4. Build a mandates[] array spanning every legislature the person
     served in.
  5. Preserve all existing entries (the seed registry's hand-curated
     entries with verified Wikidata QIDs and richer mandate detail
     stay intact).
  6. Skip entries whose surname-firstname tuple already matches an
     existing entry (idempotent re-runs).

# Name normalisation

cdep displays names in `SURNAME Firstname[ Middle…]` order — the surname
is uppercase. We keep that capitalisation in the registry's
`canonical_name` since it's the form most likely to appear verbatim in
matched Wikidata labels and other public records, but we also generate
the proper-cased variant `Firstname Surname` and add it as an alias so
the corpus's `Firstname Surname` mentions still resolve.

The kebab-case `id` is `<surname-lower>-<firstname-lower>`, ASCII-folded
and joined with `-`. Multi-token first/middle names are joined with `-`:
`PETRACHE Marin Iulian` → `petrache-marin-iulian`.

# Politeness

This tool only reads/writes local files. No network. Idempotent — re-runs
add only entries that aren't already covered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

# Reuse the existing diacritic-folding so id slugs match the matcher.
sys.path.insert(0, str(Path(__file__).parent.parent))

_PERSON_MOJIBAKE_MAP = str.maketrans(
    {
        "„": "a",
        "∫": "s",
        "˛": "t",
        "º": "s",
        "ª": "S",
        "þ": "t",
        "Þ": "T",
        "™": "S",
        "Ð": "I",
        "ð": "i",
    }
)


def _ascii_fold(s: str) -> str:
    s = s.translate(_PERSON_MOJIBAKE_MAP)
    cedilla = str.maketrans({"ţ": "t", "Ţ": "T", "ş": "s", "Ş": "S"})
    s = s.translate(cedilla)
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _ascii_fold(s).lower()).strip("-")


# cdep displays SURNAME Firstname Middle1 Middle2 ... where SURNAME is
# all-uppercase. We use that to split. Romanian surnames sometimes have
# multiple tokens (e.g. PĂȘUNE-MARIN); each surname token is uppercase.
def _split_surname_first(name: str) -> tuple[str, str]:
    """Split `SURNAME Firstname` form into (surname, given_names).

    Walks tokens left-to-right; tokens that are entirely uppercase
    (after diacritic stripping) are part of the surname. The first
    non-uppercase token starts the given-names span.

    Hyphens within a token (like `POPESCU-TĂRICEANU`) are kept as-is —
    the whole hyphenated string is one surname token.
    """
    tokens = name.strip().split()
    surname_parts: list[str] = []
    given: list[str] = []
    in_given = False
    for tok in tokens:
        # Tokens like "I." (initials) are kept with given names.
        ascii_tok = _ascii_fold(tok)
        is_upper = ascii_tok.isupper() and len(ascii_tok) > 1
        if not in_given and is_upper:
            surname_parts.append(tok)
        else:
            in_given = True
            given.append(tok)
    surname = (
        " ".join(surname_parts) if surname_parts else (tokens[0] if tokens else "")
    )
    given_str = " ".join(given) if given else ""
    return surname, given_str


def _proper_case(s: str) -> str:
    """`POPESCU-TĂRICEANU` → `Popescu-Tăriceanu`; preserves diacritics."""

    def cap(token: str) -> str:
        if not token:
            return token
        # Handle hyphenated parts: cap each segment.
        parts = token.split("-")
        return "-".join(p[:1].upper() + p[1:].lower() if p else p for p in parts)

    return " ".join(cap(t) for t in s.split())


def _person_key(name: str) -> str:
    """Identity key for deduping mandates → unique persons.

    Surname + given-name tokens, diacritic-folded + lowercased. Two
    cdep rows referring to the same person across legislatures collapse
    to the same key; spelling variants (with/without middle initial)
    might split, but that's rare and the merge step preserves both with
    aliases.
    """
    surname, given = _split_surname_first(name)
    surname_key = _ascii_fold(surname).lower()
    given_key = _ascii_fold(given).lower()
    return f"{surname_key}|{given_key}"


def _build_id(name: str, existing_ids: set[str]) -> str:
    """Kebab-case `<surname>-<given>` id, suffixed with `-2`/`-3` on collision."""
    surname, given = _split_surname_first(name)
    base = _slugify(surname) + ("-" + _slugify(given) if given else "")
    if not base:
        base = "unknown"
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _build_aliases(name: str) -> list[str]:
    """Generate the alias list from a `SURNAME Firstname Middle...` row.

    Aliases ensure the matcher's exact / case / token_set tiers all hit:
      - the source `SURNAME Firstname Middle` form (cdep verbatim)
      - the proper-cased `Firstname Middle Surname` form
      - the proper-cased `Surname Firstname Middle` form
    """
    surname, given = _split_surname_first(name)
    if not given:
        return []
    surname_proper = _proper_case(surname)
    given_proper = _proper_case(given)
    aliases: list[str] = []
    seen: set[str] = set()
    for variant in (
        f"{given_proper} {surname_proper}",
        f"{surname_proper} {given_proper}",
        name,  # cdep verbatim
    ):
        if variant and variant not in seen:
            seen.add(variant)
            aliases.append(variant)
    return aliases


def _mandate_from_cdep_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build a registry-shape mandate dict from a cdep JSONL row."""
    return {
        "role": row.get("role") or "deputat",
        "chamber": row.get("chamber") or "Camera Deputaților",
        "legislature": row.get("legislature"),
        "from": row.get("from"),
        "to": row.get("to"),
        "party": row.get("party"),
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _existing_person_keys(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Map `surname|given` key → existing entry id, so a re-run skips
    rather than duplicates.

    We also index the canonical_name and every alias against the same
    keying function — `Florin Iordache` and `Iordache Florin` both
    point at the same `iordache-florin` entry, and a cdep row for
    `IORDACHE Florin` would resolve to that same key.
    """
    out: dict[str, str] = {}

    def _key_either_order(name: str) -> str:
        """Return the canonical surname-first identity key, regardless of
        whether the input is `Surname First` or `First Surname`. We try
        both interpretations and prefer the one whose surname-token is
        more plausible (longer, or only-uppercase under fold).
        """
        # Try surname-first interpretation
        return _person_key(name)

    for e in entries:
        eid = e["id"]
        # Index the canonical_name interpretation as surname-first AND as
        # firstname-first. That way `Florin Iordache` (firstname first)
        # also indexes the `iordache|florin` key for cdep dedup.
        names = [e.get("canonical_name", ""), *(e.get("aliases") or [])]
        for n in names:
            if not n:
                continue
            # surname-first interpretation
            out.setdefault(_key_either_order(n), eid)
            # firstname-first interpretation: swap tokens
            tokens = n.split()
            if len(tokens) >= 2:
                swapped = " ".join([tokens[-1].upper()] + [t for t in tokens[:-1]])
                out.setdefault(_key_either_order(swapped), eid)
    return out


def merge(
    persons_path: Path,
    cdep_path: Path,
    *,
    write: bool = True,
) -> tuple[int, int, int]:
    """Merge cdep rows into persons.json. Returns (added, mandates_added, skipped).

    Algorithm:
      1. Build identity-key index over existing entries.
      2. Group cdep rows by identity key.
      3. For each cdep group:
         - If key matches an existing entry → skip (do not modify the
           hand-curated entry).
         - Else → mint a new entry with all the group's mandates merged
           into a single `mandates[]` array.
      4. Sort entries: keep the original ordering for existing entries,
         append new entries in ID-sorted order.
    """
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = data.get("entries") or []
    existing_keys = _existing_person_keys(entries)
    existing_ids: set[str] = {e["id"] for e in entries}

    rows = _load_jsonl(cdep_path)
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        by_key[_person_key(name)].append(row)

    added = mandates_added = skipped = 0
    new_entries: list[dict[str, Any]] = []

    for key, group in by_key.items():
        if key in existing_keys:
            skipped += 1
            continue
        # All rows in the group share the same identity. Pick the first
        # row's name as canonical (cdep verbatim form). Build mandates
        # from every row.
        sample = group[0]
        name = sample["name"]
        person_id = _build_id(name, existing_ids)
        existing_ids.add(person_id)

        surname, given = _split_surname_first(name)
        canonical = _proper_case(f"{given} {surname}".strip())
        # Some cdep rows have garbled / single-token names; skip those —
        # they almost certainly aren't usable for downstream matching.
        if not given or not surname or len(canonical) < 4:
            skipped += 1
            continue

        aliases = _build_aliases(name)
        # Drop the canonical from aliases (it's stored separately).
        aliases = [a for a in aliases if a != canonical]

        mandates = []
        for row in group:
            m = _mandate_from_cdep_row(row)
            # Dedup mandates by (legislature, role, party) within a person.
            key_m = (m.get("legislature"), m.get("role"), m.get("party"))
            if not any(
                (mm.get("legislature"), mm.get("role"), mm.get("party")) == key_m
                for mm in mandates
            ):
                mandates.append(m)
        # Sort mandates by `from` ascending.
        mandates.sort(key=lambda m: m.get("from") or "")
        mandates_added += len(mandates)

        new_entries.append(
            {
                "id": person_id,
                "canonical_name": canonical,
                "diacritic_form": canonical,
                "aliases": aliases,
                "wikidata_qid": None,
                "birth_date": None,
                "mandates": mandates,
                "homonym_disambiguation": None,
            }
        )
        added += 1

    if write and new_entries:
        # Sort new entries by id for stability across reruns.
        new_entries.sort(key=lambda e: e["id"])
        data["entries"] = entries + new_entries
        persons_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return added, mandates_added, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_cdep_into_persons",
        description="Merge data/seed_cdep.jsonl mandates into persons.json.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("src/monitorul_ii/registries/persons.json"),
        help="persons.json path",
    )
    parser.add_argument(
        "--cdep",
        type=Path,
        default=Path("data/seed_cdep.jsonl"),
        help="cdep seed JSONL path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write — just report counts",
    )
    args = parser.parse_args(argv)

    if not args.registry.is_file():
        print(f"registry not found: {args.registry}", file=sys.stderr)
        return 2
    if not args.cdep.is_file():
        print(f"cdep seed not found: {args.cdep}", file=sys.stderr)
        return 2

    added, mandates_added, skipped = merge(
        args.registry, args.cdep, write=not args.dry_run
    )
    print(
        f"added={added} entries (with {mandates_added} total mandates) "
        f"skipped={skipped} (already in registry or unparseable)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
