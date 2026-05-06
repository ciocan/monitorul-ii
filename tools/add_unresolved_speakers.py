"""Add every unresolved unique speaker from the corpus as a stub registry entry.

# Why

After the Wikidata bulk merge, `persons.json` carries ~9,000 politicians,
but only ~2,000 of them actually appear in the 5,552-doc corpus, and
~4,500 unique speakers in the corpus DON'T match any registry entry —
they're the long tail of one-mandate rural deputies, single-occurrence
ministerial witnesses, government officials testifying on specific
bills, etc. Wikidata doesn't have them because they're not famous
enough to have their own page.

The fix: walk the corpus's speaker clusters and mint a stub
`persons.json` entry for every cluster that doesn't already match an
existing registry entry. Stub entries carry:

  - `id` — kebab-case slug derived from the cluster's most-common name
  - `canonical_name` — the cluster's most-common observed `name` (or
    cleaned `raw` when no `name` is available)
  - `diacritic_form` — same as canonical_name
  - `aliases[]` — every observed raw / name variant in the cluster
    (mojibake forms, name-order swaps, with/without honorifics) so the
    matcher resolves them all to the same person_id
  - `wikidata_qid` — null (enriched later via name lookup against
    Wikidata's API by `enrich_persons_wikidata.py` once the registry is
    populated)
  - `birth_date` — null
  - `mandates[]` — empty (we don't have mandate data without external
    sources; the discoverable evidence — `year_first` / `year_last`
    from the cluster — is too coarse to be a real mandate, so we keep
    `mandates: []` and let the cdep / hand-curation path fill it in)
  - `homonym_disambiguation` — null

# Why this is safe

  1. The `id` is a deterministic function of canonical_name; re-runs on
     the same corpus produce the same ids (idempotent).
  2. The matcher already routes through every alias, so a stub entry's
     mojibake variants resolve identically to the canonical form.
  3. Schema validation passes — `wikidata_qid` and `birth_date` are
     `string | null`, `mandates` is `array` (empty allowed).
  4. Future enrichment passes (`enrich_persons_wikidata --apply`) match
     against canonical_name + aliases and augment in place; existing
     stub entries acquire QIDs over time.

# Quality filters (applied conservatively)

  - Skip clusters whose canonical form is non-canonical (denylist:
    `Din sală`, `Guvernul`, `<chair narration>`, …).
  - Skip clusters whose canonical name has fewer than 2 tokens or is
    less than 4 chars (junk speakers like single-token "X").
  - Keep clusters whose canonical name contains digits — these may be
    OCR artifacts but represent real corpus speakers we want to track.
  - Don't filter by occurrence count — single-occurrence speakers are
    real people; the registry needs them for stable URLs.

# Politeness

Local-only: reads `data/speakers_raw.jsonl` and `persons.json`,
writes `persons.json`. No network. Idempotent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Reuse the matcher's aggressive fold so cluster keys match the
# matcher's diacritic tier exactly.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from monitorul_ii.registries import (  # noqa: E402
    _strip_diacritics_aggressive,
    normalize_speaker,
)

NON_CANONICAL = (
    "<chair narration>",
    "din sală",
    "din sala",
    "din sal",
    "guvernul",
    "guvern",
    "guvernul româniei",
    "voci",
    "voci din sală",
    "voci din sala",
    "vocea din sală",
    "aplauze",
    "aplauze.",
)


_HONOR_RE = re.compile(
    r"^(?:domnu[lis]\w*|doamn[ăa]\w*|dl\.?|dna\.?|domnişoara|domnisoara)\s+",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"^(?:deputat\w*|senator\w*|ministr\w*|secretar\w*|preşedint\w*|presedint\w*|"
    r"vicepreşedint\w*|vicepresedint\w*|prim-?ministru\w*|viceprim-?ministru\w*)\s+",
    re.IGNORECASE,
)


def _peel(s: str) -> str:
    s = (s or "").strip()
    s = _HONOR_RE.sub("", s)
    s = _TITLE_RE.sub("", s)
    s = s.split(",", 1)[0].strip()
    s = re.sub(r"\s+", " ", s)
    return s.rstrip(".:").strip()


def _fold_key(s: str) -> str:
    """Orderless folded-token cluster key — matches the matcher's
    diacritic tier (so two raws that the matcher would treat as the
    same person collapse into one cluster here).
    """
    folded = _strip_diacritics_aggressive(s).lower()
    parts = re.split(r"[\s\-,]+", folded)
    cleaned = sorted(re.sub(r"[^a-z0-9]+", "", t) for t in parts)
    cleaned = [t for t in cleaned if t and len(t) >= 2]
    return "+".join(cleaned)


def _slugify(s: str) -> str:
    folded = _strip_diacritics_aggressive(s).lower()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def _build_id(canonical_name: str, existing_ids: set[str]) -> str:
    """`<surname>-<given>` kebab id; collisions get `-2`/`-3` suffix.

    Speaker.name is `Firstname Surname` order in our corpus (extractor
    convention), so the LAST whitespace-separated token is the surname.
    """
    tokens = canonical_name.strip().split()
    if not tokens:
        return "unknown"
    surname = tokens[-1]
    given = " ".join(tokens[:-1])
    base = (
        (_slugify(surname) + ("-" + _slugify(given) if given else ""))
        or _slugify(canonical_name)
        or "unknown"
    )
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _is_non_canonical(s: str) -> bool:
    low = s.lower().strip()
    return any(label in low for label in NON_CANONICAL)


def _is_junk_name(s: str) -> bool:
    """Reject names that are clearly not people."""
    s = s.strip()
    if len(s) < 4:
        return True
    tokens = s.split()
    if len(tokens) < 2:
        # Single-token "names" are usually junk (chair shorthand,
        # institution names, OCR fragments).
        return True
    # Reject if 70%+ of chars are digits/punct
    alpha = sum(1 for c in s if c.isalpha())
    if alpha < len(s) * 0.5:
        return True
    return False


def _existing_indexes(
    entries: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str]]:
    """Build (set-of-ids, fold-key→id) from the existing registry."""
    ids: set[str] = set()
    fold_to_id: dict[str, str] = {}
    for e in entries:
        ids.add(e["id"])
        for n in [
            e.get("canonical_name"),
            e.get("diacritic_form"),
            *(e.get("aliases") or []),
        ]:
            if isinstance(n, str) and n:
                # Index against both surname-first and firstname-first
                # interpretations.
                fold_to_id.setdefault(_fold_key(n), e["id"])
                fold_to_id.setdefault(_fold_key(_peel(n)), e["id"])
    return ids, fold_to_id


def _cluster_from_speakers_raw(speakers_raw_path: Path) -> dict[str, dict]:
    """Re-cluster the `data/speakers_raw.jsonl` rows under the matcher's
    aggressive fold. Returns `{fold_key: cluster_dict}` where each cluster
    has `{raws: list[(raw, count)], names: dict[name, count], total: int,
    most_common_name: str}`.
    """
    clusters: dict[str, dict] = {}
    with speakers_raw_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw = (row.get("raw") or "").strip()
            name = (row.get("name") or "").strip() or None
            count = int(row.get("count") or 0)
            if not raw:
                continue
            # Cluster key from the cleanest form available.
            key_input = name or _peel(raw)
            if not key_input:
                continue
            key = _fold_key(key_input)
            if not key:
                continue
            cl = clusters.setdefault(
                key,
                {
                    "raws": [],
                    "names": defaultdict(int),
                    "total": 0,
                    "key_input_samples": [],
                },
            )
            cl["raws"].append((raw, count))
            if name:
                cl["names"][name] += count
            cl["total"] += count
            cl["key_input_samples"].append(key_input)
    # Pick the most-common name per cluster as canonical.
    for cl in clusters.values():
        if cl["names"]:
            cl["most_common_name"] = max(cl["names"].items(), key=lambda kv: kv[1])[0]
        else:
            # No `name` ever extracted — fall back to peeled raw.
            best_raw = max(cl["raws"], key=lambda rc: rc[1])[0]
            cl["most_common_name"] = _peel(best_raw)
    return clusters


def add_unresolved(
    persons_path: Path,
    speakers_raw_path: Path,
    *,
    write: bool = True,
) -> tuple[int, int, int, int]:
    """Add stub entries for every unresolved cluster.

    Returns `(added, skipped_resolved, skipped_non_canonical, skipped_junk)`.
    """
    data = json.loads(persons_path.read_text(encoding="utf-8"))
    entries: list[dict[str, Any]] = data.get("entries") or []
    existing_ids, fold_to_id = _existing_indexes(entries)

    clusters = _cluster_from_speakers_raw(speakers_raw_path)
    print(
        f"clustered {len(clusters)} unique fold-keys from corpus speakers",
        file=sys.stderr,
    )

    added = skipped_resolved = skipped_non_canon = skipped_junk = 0
    new_entries: list[dict[str, Any]] = []

    # Sort clusters by total mention count desc — most-frequent first
    # so the kebab-id collision suffixes go to less-frequent clusters.
    sorted_clusters = sorted(clusters.items(), key=lambda kv: -kv[1]["total"])

    for fold_key, cl in sorted_clusters:
        canonical = cl["most_common_name"].strip()

        if _is_non_canonical(canonical):
            skipped_non_canon += 1
            continue
        if _is_junk_name(canonical):
            skipped_junk += 1
            continue

        # Already in the registry?
        if fold_key in fold_to_id:
            skipped_resolved += 1
            continue
        # Try the full matcher (catches fuzzy + token-set matches that
        # the fold-key alone might miss).
        # Pick the most-frequent raw as the matcher input.
        best_raw = max(cl["raws"], key=lambda rc: rc[1])[0]
        eid, _via = normalize_speaker(best_raw)
        if eid is not None:
            skipped_resolved += 1
            continue

        # Mint a new entry.
        person_id = _build_id(canonical, existing_ids)
        existing_ids.add(person_id)

        # Aliases: every observed raw / name form (un-peeled), dedup'd.
        # We deliberately DO NOT add `_peel(raw)` as an alias even when
        # the cluster carries honorific'd surface forms — peeled aliases
        # tend to collide with canonical entries on the matcher's
        # diacritic-folded homonym index. Concrete bug: a polluted
        # cluster keyed on "Domnule Nicolae Văcăroiu" would add the
        # peeled "Nicolae Văcăroiu" as an alias, which then shares a
        # diacritic key with the canonical `vacaroiu-nicolae` entry —
        # the matcher refuses to disambiguate the homonym and returns
        # null for both polluted and clean forms. The matcher already
        # peels honorifics + titles at lookup time, so polluted-form
        # raws still hit the clean canonical entry directly.
        alias_set: dict[str, None] = {}  # ordered set
        for raw, _count in cl["raws"]:
            if raw and raw != canonical:
                alias_set[raw] = None
        for name in cl["names"]:
            if name and name != canonical:
                alias_set[name] = None

        new_entries.append(
            {
                "id": person_id,
                "canonical_name": canonical,
                "diacritic_form": canonical,
                "aliases": list(alias_set.keys()),
                "wikidata_qid": None,
                "birth_date": None,
                "mandates": [],
                "homonym_disambiguation": None,
            }
        )
        # Index the new entry's fold keys so subsequent clusters that
        # collide (rare but possible with our aggressive fold) skip.
        fold_to_id[fold_key] = person_id
        for alias in alias_set:
            fold_to_id.setdefault(_fold_key(alias), person_id)
            fold_to_id.setdefault(_fold_key(_peel(alias)), person_id)
        added += 1

    if write and new_entries:
        # Sort new entries by id for stability across reruns.
        new_entries.sort(key=lambda e: e["id"])
        data["entries"] = entries + new_entries
        persons_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return added, skipped_resolved, skipped_non_canon, skipped_junk


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="add_unresolved_speakers",
        description="Add stub registry entries for every unresolved corpus speaker.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("src/monitorul_ii/registries/persons.json"),
    )
    parser.add_argument(
        "--speakers-raw",
        type=Path,
        default=Path("data/speakers_raw.jsonl"),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write — just report counts.",
    )
    args = parser.parse_args(argv)

    if not args.registry.is_file():
        print(f"registry not found: {args.registry}", file=sys.stderr)
        return 2
    if not args.speakers_raw.is_file():
        print(
            f"speakers_raw not found: {args.speakers_raw} "
            "(run `tools.aggregate_speakers` first)",
            file=sys.stderr,
        )
        return 2

    added, sk_resolved, sk_non_canon, sk_junk = add_unresolved(
        args.registry, args.speakers_raw, write=not args.dry_run
    )
    print(
        f"\nadded={added} stub entries\n"
        f"  skipped (already in registry):     {sk_resolved}\n"
        f"  skipped (non-canonical labels):    {sk_non_canon}\n"
        f"  skipped (junk / single-token):     {sk_junk}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
