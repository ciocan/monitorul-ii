"""Sample stratified speeches from the corpus for the discourse-analysis
calibration smoke.

Distinct from `select_pilot_speeches.py`: that script targets ten registry-
tied politicians for the gold-comparison pilot anchor; THIS script walks the
full corpus, applies a substantive filter, and produces a broader sample
across years / speakers / document types — used to confirm Flash-Lite
calibration holds before the corpus-wide indexer pass.

Walks every `*.extraction.json` under `pdfs/`, filters to `plenary_stenogram`
+ `plenary_joint_session` sidecars, collects every speech activity meeting the
substantive-text-length floor (matches the indexer's
`SUBSTANTIVE_TEXT_LENGTH = 100`), and samples a target population using one
of three strategies:

  - `random`           uniform random sample with deterministic seed (default).
                        Reflects the corpus's natural year-distribution skew.
  - `year-stratified`  splits by metadata.year, samples ~equal counts per
                        year. Good for surfacing era-specific drift
                        (mojibake-2000s vs modern 2020s) at small sample
                        sizes where uniform random would leave thin coverage
                        of pre-2010 speeches.
  - `decade-stratified`  same idea, decade buckets.

Output JSONL shape matches `select_pilot_speeches.py` so `pilot_benchmark.py`
runs against either file unchanged.

Usage:

    uv run python tools/sample_speeches.py \\
        --out validation/calibration_500.jsonl \\
        --target-size 500 \\
        --strategy year-stratified \\
        --seed 42 \\
        --min-words 100 --max-words 800
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator


def _word_count(text: str) -> int:
    return len(text.split())


def _walk_speeches(
    pdfs_root: Path,
) -> Iterator[tuple[Path, dict[str, Any], int, int, dict, dict]]:
    """Yield every speech activity across the corpus.

    Mirrors `select_pilot_speeches._walk_speeches` but exposes the same
    intermediates so the emit-record helper can be shared.
    """
    for path in sorted(pdfs_root.glob("*.extraction.json")):
        try:
            sidecar = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] {path.name}: {exc}", file=sys.stderr)
            continue
        if sidecar.get("document_type") not in {
            "plenary_stenogram",
            "plenary_joint_session",
        }:
            continue
        body = sidecar.get("body") or {}
        for ai, agenda_item in enumerate(body.get("agenda_items") or []):
            for ji, activity in enumerate(agenda_item.get("activities") or []):
                if activity.get("type") != "speech":
                    continue
                yield path, sidecar, ai, ji, agenda_item, activity


def _emit_record(
    *,
    extraction_path: Path,
    sidecar: dict,
    agenda_idx: int,
    activity_idx: int,
    agenda_item: dict,
    activity: dict,
) -> dict:
    speaker = activity.get("speaker") or {}
    metadata = sidecar.get("metadata") or {}
    text = activity.get("text") or ""
    src_span = activity.get("source_span") or {}
    return {
        "speech_id": activity.get("id"),
        "document_id": sidecar.get("document_id"),
        # `tier` is a placeholder so the field shape matches the pilot anchor's
        # JSONL — pilot_benchmark.py groups results by tier and handles `null`
        # / unknown values, but the field's presence keeps the schema
        # consistent across calibration + pilot inputs.
        "tier": "calibration",
        "speaker": {
            "id": speaker.get("person_id"),
            "name": speaker.get("name"),
            "raw": speaker.get("raw"),
            "party_group": speaker.get("party_group"),
        },
        "metadata": {
            "chamber": metadata.get("chamber"),
            "session_date": metadata.get("session_date"),
            "year": metadata.get("year"),
            "legislature": metadata.get("legislature"),
            "delivery_mode": activity.get("delivery_mode"),
        },
        "agenda_title": agenda_item.get("title"),
        "agenda_idx": agenda_idx,
        "activity_idx": activity_idx,
        "text": text,
        "text_length_chars": len(text),
        "text_length_words": _word_count(text),
        "source_span_chars": src_span.get("chars"),
        "extraction_path": str(extraction_path),
    }


def _is_canonical_speaker(speaker: dict) -> bool:
    """Skip non-canonical narrator / institutional / chorus labels.

    `<chair narration>`, `Din sală`, `Voci`, `Guvernul`, etc. are explicitly
    not codable as discourse-analysis speeches; they're either narrator-
    embedded or institutional placeholders. Filtering them at the sampler
    avoids polluting the calibration sample with un-codable cases that
    would surface as Hawkins-noise / DQI-noise downstream.
    """
    raw = (speaker or {}).get("raw") or ""
    name = (speaker or {}).get("name") or ""
    candidate = (name or raw).strip().lower()
    if not candidate:
        return False
    denylist_substrings = (
        "<chair narration>",
        "din sală",
        "din sala",
        "voci",
        "guvernul",
        "aplauze",
        "rumoare",
    )
    for s in denylist_substrings:
        if s in candidate:
            return False
    return True


def collect_pool(
    pdfs_root: Path,
    *,
    min_words: int,
    max_words: int,
) -> list[dict]:
    """Walk the corpus once, return every substantive canonical-speaker speech."""
    pool: list[dict] = []
    walked = 0
    for path, sidecar, ai, ji, agenda_item, activity in _walk_speeches(pdfs_root):
        walked += 1
        speaker = activity.get("speaker") or {}
        if not _is_canonical_speaker(speaker):
            continue
        text = activity.get("text") or ""
        words = _word_count(text)
        if not (min_words <= words <= max_words):
            continue
        speech_id = activity.get("id")
        if speech_id is None:
            continue
        pool.append(
            _emit_record(
                extraction_path=path,
                sidecar=sidecar,
                agenda_idx=ai,
                activity_idx=ji,
                agenda_item=agenda_item,
                activity=activity,
            )
        )
    print(
        f"[info] walked {walked} speech activities; pool size after filters: {len(pool)}",
        file=sys.stderr,
    )
    return pool


def sample_random(pool: list[dict], target_size: int, *, seed: int) -> list[dict]:
    rng = random.Random(seed)
    if target_size >= len(pool):
        return list(pool)
    return rng.sample(pool, target_size)


def sample_year_stratified(
    pool: list[dict], target_size: int, *, seed: int
) -> list[dict]:
    """Sample ~equal counts per year; final size matches target_size as
    closely as possible without inventing speeches for years that don't have
    enough population.

    Strategy: bucket by `metadata.year`; compute per-year quota
    `floor(target_size / n_years)`; sample within each bucket; if the
    accumulated total is below `target_size`, fill with uniform random from
    the residual pool.
    """
    rng = random.Random(seed)
    by_year: dict[int | None, list[dict]] = defaultdict(list)
    for r in pool:
        y = (r.get("metadata") or {}).get("year")
        by_year[y].append(r)
    years = sorted(y for y in by_year if y is not None)
    if not years:
        return sample_random(pool, target_size, seed=seed)
    per_year = max(1, target_size // len(years))
    picked: list[dict] = []
    picked_ids: set[str] = set()
    for y in years:
        bucket = list(by_year[y])
        rng.shuffle(bucket)
        for r in bucket[:per_year]:
            picked.append(r)
            picked_ids.add(r["speech_id"])
    # Residual fill — random from speeches not yet picked
    if len(picked) < target_size:
        residual = [r for r in pool if r["speech_id"] not in picked_ids]
        rng.shuffle(residual)
        for r in residual:
            if len(picked) >= target_size:
                break
            picked.append(r)
            picked_ids.add(r["speech_id"])
    # Trim down if we overshot (per_year=1 and many years can produce >target)
    if len(picked) > target_size:
        rng.shuffle(picked)
        picked = picked[:target_size]
    return picked


def sample_decade_stratified(
    pool: list[dict], target_size: int, *, seed: int
) -> list[dict]:
    """Decade-bucket sample; same fill strategy as year-stratified."""
    rng = random.Random(seed)
    by_decade: dict[int | None, list[dict]] = defaultdict(list)
    for r in pool:
        y = (r.get("metadata") or {}).get("year")
        d = (y // 10) * 10 if isinstance(y, int) else None
        by_decade[d].append(r)
    decades = sorted(d for d in by_decade if d is not None)
    if not decades:
        return sample_random(pool, target_size, seed=seed)
    per_decade = max(1, target_size // len(decades))
    picked: list[dict] = []
    picked_ids: set[str] = set()
    for d in decades:
        bucket = list(by_decade[d])
        rng.shuffle(bucket)
        for r in bucket[:per_decade]:
            picked.append(r)
            picked_ids.add(r["speech_id"])
    if len(picked) < target_size:
        residual = [r for r in pool if r["speech_id"] not in picked_ids]
        rng.shuffle(residual)
        for r in residual:
            if len(picked) >= target_size:
                break
            picked.append(r)
            picked_ids.add(r["speech_id"])
    if len(picked) > target_size:
        rng.shuffle(picked)
        picked = picked[:target_size]
    return picked


_STRATEGIES = {
    "random": sample_random,
    "year-stratified": sample_year_stratified,
    "decade-stratified": sample_decade_stratified,
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--out",
        type=Path,
        default=Path("validation/calibration_500.jsonl"),
        help="Output JSONL path (default: validation/calibration_500.jsonl).",
    )
    p.add_argument("--pdfs", type=Path, default=Path("pdfs"))
    p.add_argument("--target-size", type=int, default=500)
    p.add_argument(
        "--strategy",
        type=str,
        default="year-stratified",
        choices=list(_STRATEGIES),
        help="Sampling strategy. Default year-stratified — broad coverage.",
    )
    p.add_argument(
        "--min-words",
        type=int,
        default=100,
        help="Minimum word count (default: 100, matches indexer is_substantive cutoff).",
    )
    p.add_argument(
        "--max-words",
        type=int,
        default=800,
        help="Maximum word count (default: 800, caps token cost per call).",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + sample but do not write the output file.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.pdfs.exists():
        print(f"[error] --pdfs {args.pdfs} does not exist", file=sys.stderr)
        return 2
    if args.target_size < 1:
        print(
            f"[error] --target-size must be >= 1 (got {args.target_size})",
            file=sys.stderr,
        )
        return 2

    pool = collect_pool(
        args.pdfs,
        min_words=args.min_words,
        max_words=args.max_words,
    )
    if not pool:
        print(
            "[error] empty pool — no substantive speeches matched filters",
            file=sys.stderr,
        )
        return 1

    sampler = _STRATEGIES[args.strategy]
    picked = sampler(pool, args.target_size, seed=args.seed)

    print(
        f"[info] strategy={args.strategy}  picked={len(picked)} of pool {len(pool)}",
        file=sys.stderr,
    )
    by_year = Counter(r.get("metadata", {}).get("year") for r in picked)
    print("[info] per-year picked:", file=sys.stderr)
    for y, n in sorted(by_year.items(), key=lambda kv: (kv[0] is None, kv[0])):
        print(f"  {y}  {n}", file=sys.stderr)

    distinct_speakers = len({(r.get("speaker") or {}).get("id") for r in picked})
    print(
        f"[info] distinct speaker IDs in sample: {distinct_speakers}", file=sys.stderr
    )

    if args.dry_run:
        print("[info] --dry-run; not writing", file=sys.stderr)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[info] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
