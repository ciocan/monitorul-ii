"""Aggregate raw speaker strings from extracted sidecars.

Walks `pdfs/*.extraction.json` (or any directory of sidecars), pulls every
Speaker dict out of the body, and emits two JSONL files for the
persons.json bootstrap pipeline (Q4 of `docs/elasticsearch-indexing.md`):

  data/speakers_raw.jsonl
    One row per distinct (raw, name) pair: `{raw, name, count, sample_doc_ids}`.

  data/speaker_clusters.jsonl
    One row per name-form-normalized cluster:
    `{cluster_id, normalized, raws, names, total_count, year_first, year_last}`.

Run via `python -m tools.aggregate_speakers <sidecar-dir>` or directly.

The aggregation is deterministic and idempotent: running it twice on the
same corpus produces byte-identical output (rows are sorted by count
desc, ties broken by `normalized` ascending).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Iterator

# Re-use the registries' diacritic-folding so cluster ids match the
# matcher's behaviour.
from monitorul_ii.registries import _tokenise


SPEAKER_KEYS = frozenset({"raw", "name", "title", "role", "party_group", "person_id"})


def _walk_speakers(
    o: Any, doc_id: str, year: int | None
) -> Iterator[tuple[str, str | None, str, int | None]]:
    """Yield `(raw, name, doc_id, year)` for every Speaker in the body.

    Recognises the canonical Speaker shape by structural duck-typing
    (presence of all six required keys). Handles arbitrary nesting under
    `body` — agenda activities, interpellations, roster entries, signatures,
    etc.
    """
    if isinstance(o, dict):
        if SPEAKER_KEYS.issubset(o.keys()):
            raw = o.get("raw")
            name = o.get("name")
            if isinstance(raw, str) and raw.strip():
                yield (
                    raw.strip(),
                    name if isinstance(name, str) else None,
                    doc_id,
                    year,
                )
            return  # Speaker leaves don't recurse into their own keys
        for v in o.values():
            yield from _walk_speakers(v, doc_id, year)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_speakers(v, doc_id, year)


def _cluster_key(name: str | None, raw: str) -> str:
    """Stable cluster id for grouping (name, raw) pairs.

    Prefers `name` (the extractor's already-cleaned form) when present; falls
    back to a honorific-stripped pass over the raw. The result is a
    diacritic-folded, lowercased token-set joined with `+` so reorderings
    (`Iordache Florin` vs `Florin Iordache`) collapse.
    """
    text = (name or _strip_honorific_from_raw(raw)).strip()
    tokens = sorted(_tokenise(text))
    return "+".join(tokens) if tokens else "_unknown"


# Honorifics + parliamentary titles to peel off the front of raw strings
# when no `name` is present. Keep the list short — the matcher does the
# heavy lifting; here we just want a clusterable normalized form.
_HONORIFIC_PREFIXES = (
    "Domnul",
    "Doamna",
    "Domnişoara",
    "Domnișoara",
    "domnul",
    "doamna",
    "Dl.",
    "Dna.",
    "Dl",
    "Dna",
)
_TITLE_TOKENS = (
    "deputat",
    "deputata",
    "deputatul",
    "senator",
    "senatorul",
    "senatoare",
    "ministru",
    "ministrul",
    "secretar",
    "preşedinte",
    "președinte",
    "vicepreşedinte",
    "vicepreședinte",
    "viceprim-ministru",
    "prim-ministru",
)


def _strip_honorific_from_raw(raw: str) -> str:
    """Best-effort honorific + title strip used only for clustering.

    The runtime matcher (registries.normalize_speaker) does the real
    parsing; this is just to keep clusters from fragmenting between
    `Domnul X` / `Domnul deputat X` / `X`.
    """
    s = raw.strip()
    for h in _HONORIFIC_PREFIXES:
        if s.startswith(h + " "):
            s = s[len(h) + 1 :].strip()
            break
    for t in _TITLE_TOKENS:
        if s.lower().startswith(t.lower() + " "):
            s = s[len(t) + 1 :].strip()
            break
    # Drop trailing role clauses like `, vicepreședinte al Senatului`.
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s


def aggregate(
    sidecar_paths: Iterable[Path],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Walk sidecars and produce (raw_rows, cluster_rows).

    Pure function — easy to call from tests with a small in-memory list.
    """
    raw_counts: dict[tuple[str, str | None], dict[str, Any]] = {}
    cluster_to_raws: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "raws": defaultdict(int),
            "names": set(),
            "year_first": None,
            "year_last": None,
            "total_count": 0,
        }
    )

    for path in sidecar_paths:
        try:
            sc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        body = sc.get("body")
        if not isinstance(body, dict):
            continue
        doc_id = sc.get("document_id") or path.stem
        meta = sc.get("metadata") or {}
        year = meta.get("year") if isinstance(meta.get("year"), int) else None
        for raw, name, did, yr in _walk_speakers(body, doc_id, year):
            key = (raw, name)
            slot = raw_counts.setdefault(
                key,
                {"raw": raw, "name": name, "count": 0, "sample_doc_ids": []},
            )
            slot["count"] += 1
            if len(slot["sample_doc_ids"]) < 3 and did not in slot["sample_doc_ids"]:
                slot["sample_doc_ids"].append(did)

            cid = _cluster_key(name, raw)
            cl = cluster_to_raws[cid]
            cl["raws"][raw] += 1
            if name:
                cl["names"].add(name)
            cl["total_count"] += 1
            if yr is not None:
                if cl["year_first"] is None or yr < cl["year_first"]:
                    cl["year_first"] = yr
                if cl["year_last"] is None or yr > cl["year_last"]:
                    cl["year_last"] = yr

    raw_rows = sorted(
        raw_counts.values(),
        key=lambda r: (-r["count"], r["raw"]),
    )

    cluster_rows: list[dict[str, Any]] = []
    for cid, cl in cluster_to_raws.items():
        names_sorted = sorted(cl["names"])
        normalized = (
            names_sorted[0]
            if names_sorted
            else _strip_honorific_from_raw(next(iter(cl["raws"])))
        )
        cluster_rows.append(
            {
                "cluster_id": cid,
                "normalized": normalized,
                "raws": [
                    {"raw": r, "count": c}
                    for r, c in sorted(
                        cl["raws"].items(), key=lambda kv: (-kv[1], kv[0])
                    )
                ],
                "names": names_sorted,
                "total_count": cl["total_count"],
                "year_first": cl["year_first"],
                "year_last": cl["year_last"],
            }
        )
    cluster_rows.sort(key=lambda r: (-r["total_count"], r["cluster_id"]))
    return raw_rows, cluster_rows


def _resolve_inputs(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for p in paths:
        if p.is_dir():
            for child in sorted(p.glob("*.extraction.json")):
                if child not in seen:
                    seen.add(child)
                    out.append(child)
        elif p.is_file() and p.name.endswith(".extraction.json"):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aggregate_speakers",
        description=(
            "Walk extraction sidecars and emit speakers_raw.jsonl + "
            "speaker_clusters.jsonl for the persons.json bootstrap loop."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Sidecar JSON files or directories (non-recursive).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data"),
        help="Output directory for the JSONL files (default: ./data).",
    )
    args = parser.parse_args(argv)

    sidecars = _resolve_inputs(list(args.paths))
    if not sidecars:
        print("no .extraction.json files found", file=sys.stderr)
        return 0

    raw_rows, cluster_rows = aggregate(sidecars)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw_out = args.out_dir / "speakers_raw.jsonl"
    cluster_out = args.out_dir / "speaker_clusters.jsonl"

    with raw_out.open("w", encoding="utf-8") as f:
        for row in raw_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with cluster_out.open("w", encoding="utf-8") as f:
        for row in cluster_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(
        f"wrote {len(raw_rows)} raw rows -> {raw_out}",
        file=sys.stderr,
    )
    print(
        f"wrote {len(cluster_rows)} clusters -> {cluster_out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
