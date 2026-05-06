"""Quick inspector for resolving an unresolved Speaker raw against the corpus.

Usage:

    uv run python -m tools.inspect_speaker "<raw-string>" [--corpus pdfs] [--limit 5]

For each match, prints:

  - the sidecar document_id, year, chamber
  - the raw + name as recorded
  - a ~200-char excerpt of the surrounding context (from the sidecar's
    raw_markdown_path when the file is reachable)

Used during the persons.json hand-resolution pass: pick a long-tail entry
from `data/persons_unresolved.jsonl`, run this to see which docs / years
the raw appears in, decide whether to add a registry entry (with the
right mandate dates) or mark the speaker as `non_canonical`.

Stand-alone — does not depend on the registry matcher itself, since the
whole point is to inspect strings that are NOT yet resolvable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator


SPEAKER_KEYS = frozenset({"raw", "name", "title", "role", "party_group", "person_id"})


def _walk_speakers(o, path="") -> Iterator[tuple[str, dict]]:
    if isinstance(o, dict):
        if SPEAKER_KEYS.issubset(o.keys()):
            yield (path, o)
            return
        for k, v in o.items():
            yield from _walk_speakers(v, f"{path}.{k}")
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _walk_speakers(v, f"{path}[{i}]")


def _excerpt(md_path: Path, raw: str, *, ctx: int = 200) -> str | None:
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    idx = text.find(raw)
    if idx < 0:
        # Try a shorter substring (first 30 chars)
        probe = raw[:30]
        if probe:
            idx = text.find(probe)
        if idx < 0:
            return None
    start = max(0, idx - ctx // 2)
    end = min(len(text), idx + len(raw) + ctx // 2)
    snippet = text[start:end]
    return snippet.replace("\n", " ⏎ ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspect_speaker",
        description="Show context excerpts for an unresolved speaker raw string.",
    )
    parser.add_argument(
        "raw",
        type=str,
        help="The raw speaker string to look up (or a substring of it).",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("pdfs"),
        help="Directory containing *.extraction.json sidecars (default: ./pdfs).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max number of context excerpts to print (default: 5).",
    )
    parser.add_argument(
        "--substring",
        action="store_true",
        help="Match raw as a substring (default: exact match).",
    )
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        print(f"corpus dir not found: {args.corpus}", file=sys.stderr)
        return 2

    target = args.raw
    hits: list[dict] = []
    by_year: dict[int, int] = defaultdict(int)

    for sidecar in sorted(args.corpus.glob("*.extraction.json")):
        try:
            sc = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        body = sc.get("body")
        if not isinstance(body, dict):
            continue
        for path_str, sp in _walk_speakers(body):
            raw = sp.get("raw") or ""
            if not isinstance(raw, str):
                continue
            matched = (raw == target) if not args.substring else (target in raw)
            if not matched:
                continue
            hits.append(
                {
                    "sidecar": sidecar,
                    "doc_id": sc.get("document_id"),
                    "year": (sc.get("metadata") or {}).get("year"),
                    "chamber": (sc.get("metadata") or {}).get("chamber"),
                    "speaker_path": path_str,
                    "speaker": sp,
                }
            )
            year = (sc.get("metadata") or {}).get("year")
            if isinstance(year, int):
                by_year[year] += 1

    if not hits:
        print(f"no speakers matched {target!r}", file=sys.stderr)
        return 1

    print(f"{len(hits)} hits across {len(by_year)} years", file=sys.stderr)
    for year in sorted(by_year):
        print(f"  {year}: {by_year[year]}", file=sys.stderr)
    print("", file=sys.stderr)

    for h in hits[: args.limit]:
        sidecar: Path = h["sidecar"]
        md_path = sidecar.with_name(sidecar.stem.replace(".extraction", "") + ".md")
        excerpt = _excerpt(md_path, h["speaker"].get("raw") or target)
        sp = h["speaker"]
        print(
            f"=== {h['doc_id']}  ({h['year']}, {h['chamber']})  {h['speaker_path']}",
            flush=True,
        )
        print(
            f"    raw:        {sp.get('raw')!r}\n"
            f"    name:       {sp.get('name')!r}\n"
            f"    title:      {sp.get('title')!r}\n"
            f"    role:       {sp.get('role')!r}\n"
            f"    party:      {sp.get('party_group')!r}\n"
            f"    person_id:  {sp.get('person_id')!r}",
            flush=True,
        )
        if excerpt:
            print(f"    excerpt:    …{excerpt}…", flush=True)
        else:
            print("    (no MD excerpt — raw_markdown_path unreachable)", flush=True)
        print("", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
