"""Merge `data/seed_wikidata_bulk.jsonl` rows into `persons.json`.

Each Wikidata row is one person (not one mandate); the merge is simpler
than `merge_cdep_into_persons.py`:

  1. De-dupe vs the existing registry by `wikidata_qid` first (every
     existing entry with a QID skips). Then by name-form key (so a
     hand-curated entry without a QID still receives the QID + birth_date
     from Wikidata when the names match).
  2. New entries arrive with `mandates: []` — Wikidata's P39 (position
     held) is inconsistently populated and the position-QIDs don't map
     cleanly to Romanian legislatures, so we don't try to derive
     mandates here. The cdep / hand-resolution path fills mandates in.
  3. Aliases auto-generated from the en + ro labels (handles
     diacritic-stripped vs comma-diacritic forms, and Surname-First vs
     First-Surname order via the existing _build_aliases logic).

# Idempotency

Re-runs are no-ops once the registry is populated:
  - QID match → skip
  - Name match (no QID) → augment with QID + birth_date from Wikidata
  - Otherwise → mint new entry

# Politeness

Local-only; reads two files and writes one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

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


def _name_key(name: str) -> str:
    """Identity key based on the orderless ASCII-folded token set.

    `Florin Iordache` and `Iordache Florin` produce the same key. This
    is what we use to detect that a Wikidata entry corresponds to an
    existing registry entry (regardless of which order the registry
    stored the canonical_name in).
    """
    folded = _ascii_fold(name).lower()
    parts = re.split(r"[\s\-]+", folded)
    cleaned = sorted(re.sub(r"[^a-z0-9]+", "", t) for t in parts)
    cleaned = [t for t in cleaned if t and len(t) >= 2]
    return "+".join(cleaned)


def _build_id(name: str, existing_ids: set[str]) -> str:
    """`<surname>-<given>` kebab id with `-2`/`-3` suffix on collision.

    For Wikidata-sourced names we don't have a SURNAME-uppercase signal,
    so we treat the LAST whitespace-separated token as the surname (the
    Wikidata convention is `First Last`).
    """
    tokens = name.strip().split()
    if not tokens:
        return "unknown"
    surname = tokens[-1]
    given = " ".join(tokens[:-1])
    base = (
        (_slugify(surname) + ("-" + _slugify(given) if given else ""))
        or _slugify(name)
        or "unknown"
    )
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _build_aliases(label_en: str, label_ro: str | None) -> list[str]:
    """Generate alias variants for Wikidata labels.

    - en label as canonical
    - ro label (if different) as alias
    - reversed-token form `Surname First` (cdep-style)
    - ASCII-stripped form (helps mojibake-tolerant matching)
    """
    aliases: list[str] = []
    seen: set[str] = set()

    def _add(s: str | None) -> None:
        if not s:
            return
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in seen:
            seen.add(s)
            aliases.append(s)

    if label_ro:
        _add(label_ro)
    # Reversed-token form
    tokens = (label_en or "").split()
    if len(tokens) >= 2:
        reversed_form = " ".join([tokens[-1]] + tokens[:-1])
        _add(reversed_form)
    # ASCII-folded form (only if it differs from the canonical)
    folded_en = _ascii_fold(label_en)
    if folded_en != label_en:
        _add(folded_en)
    if label_ro:
        folded_ro = _ascii_fold(label_ro)
        if folded_ro != label_ro:
            _add(folded_ro)
    return aliases


def _existing_indexes(
    entries: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """Return (qids, name_key→id, ids).

    `qids` — every wikidata_qid already in the registry.
    `name_key→id` — every canonical_name + alias keyed by `_name_key`.
    `ids` — set of registry ids.
    """
    qids: set[str] = set()
    name_to_id: dict[str, str] = {}
    ids: set[str] = set()
    for e in entries:
        ids.add(e["id"])
        if isinstance(e.get("wikidata_qid"), str):
            qids.add(e["wikidata_qid"])
        for n in [
            e.get("canonical_name"),
            e.get("diacritic_form"),
            *(e.get("aliases") or []),
        ]:
            if isinstance(n, str) and n:
                name_to_id.setdefault(_name_key(n), e["id"])
    return qids, name_to_id, ids


def merge(
    persons_path: Path,
    bulk_path: Path,
    *,
    write: bool = True,
) -> tuple[int, int, int]:
    """Merge bulk Wikidata rows into persons.json.

    Returns (added, augmented, skipped).
      - added: brand-new entries minted from Wikidata.
      - augmented: existing entries that received a wikidata_qid
        (and birth_date) from a Wikidata row that matched on name.
      - skipped: rows that already had a registry-side QID match
        OR whose Wikidata label was empty.
    """
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = data.get("entries") or []
    qids, name_to_id, ids = _existing_indexes(entries)
    by_id = {e["id"]: e for e in entries}

    added = augmented = skipped = 0
    new_entries: list[dict[str, Any]] = []

    with bulk_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            qid = row.get("qid")
            label_en = (row.get("label_en") or "").strip()
            label_ro = (row.get("label_ro") or None) or None
            birth_raw = row.get("birth_date")
            birth = (
                birth_raw[:10]
                if isinstance(birth_raw, str) and len(birth_raw) >= 10
                else None
            )

            if not qid or not label_en or label_en == qid:
                # Wikidata sometimes returns the QID as the label when
                # there's no en label — skip those.
                skipped += 1
                continue

            if qid in qids:
                skipped += 1
                continue

            # Match by name: was this person already in the registry
            # under a different (or no) QID?
            key_en = _name_key(label_en)
            existing_id = name_to_id.get(key_en)
            if not existing_id and label_ro:
                existing_id = name_to_id.get(_name_key(label_ro))

            if existing_id:
                e = by_id[existing_id]
                if not e.get("wikidata_qid"):
                    e["wikidata_qid"] = qid
                    qids.add(qid)
                    if birth and not e.get("birth_date"):
                        e["birth_date"] = birth
                    augmented += 1
                else:
                    skipped += 1
                continue

            # Mint a new entry.
            person_id = _build_id(label_en, ids)
            ids.add(person_id)
            qids.add(qid)
            aliases = _build_aliases(label_en, label_ro)
            new_entry = {
                "id": person_id,
                "canonical_name": label_en,
                "diacritic_form": label_ro or label_en,
                "aliases": aliases,
                "wikidata_qid": qid,
                "birth_date": birth,
                "mandates": [],
                "homonym_disambiguation": None,
            }
            new_entries.append(new_entry)
            # Index the new entry into both name_to_id (for later
            # row-matching) AND by_id (so the lookup branch above can
            # find it on a later iteration without KeyError).
            by_id[person_id] = new_entry
            for n in [label_en, label_ro, *aliases]:
                if n:
                    name_to_id.setdefault(_name_key(n), person_id)
            added += 1

    if write and (new_entries or augmented):
        new_entries.sort(key=lambda e: e["id"])
        data["entries"] = entries + new_entries
        persons_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return added, augmented, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge_wikidata_bulk",
        description="Merge Wikidata bulk JSONL into persons.json.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("src/monitorul_ii/registries/persons.json"),
    )
    parser.add_argument(
        "--bulk",
        type=Path,
        default=Path("data/seed_wikidata_bulk.jsonl"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.registry.is_file():
        print(f"registry not found: {args.registry}", file=sys.stderr)
        return 2
    if not args.bulk.is_file():
        print(f"bulk seed not found: {args.bulk}", file=sys.stderr)
        return 2

    added, augmented, skipped = merge(args.registry, args.bulk, write=not args.dry_run)
    print(
        f"added={added} augmented={augmented} skipped={skipped}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
