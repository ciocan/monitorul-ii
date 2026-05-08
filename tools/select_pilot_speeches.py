"""Select a stratified pilot sample of speeches for the discourse-analysis Romanian-competence benchmark.

Walks every `*.extraction.json` under `pdfs/`, finds speech activities whose
`speaker.person_id` matches one of three stratified tiers (known-populist,
moderate-establishment, register-shifters), filters by word count, and writes
the picked speeches to a JSONL file the pilot harness consumes.

The tiers and their politician slugs are defined inline below — they reflect
the schema doc's gold-sample stratification (Q8 of `docs/discourse-analysis-schema.md`)
adapted for the 10-speech pilot (no random tier; the random tier ships with
the larger 200-speech gold sample later).

Usage:

    uv run python tools/select_pilot_speeches.py \
        --out validation/pilot_speeches.jsonl \
        --pdfs pdfs/ \
        --per-populist 1 --per-moderate 1 --per-shifter 1 \
        --min-words 100 --max-words 800 \
        --seed 42

The default `--out` points at `validation/` (committed to repo) rather than
`data/` (gitignored): the pilot input is benchmark substrate, not transient
data, and pinning it gives downstream benchmark results lasting provenance.

Output JSONL shape (one line per selected speech):

    {
      "speech_id": "mo://2024/PII/3#agenda/2/activity/41",
      "document_id": "mo://2024/PII/3",
      "tier": "populist | moderate | register_shift",
      "speaker": {"id": "...", "name": "...", "raw": "...", "party_group": "..."},
      "metadata": {"chamber": "...", "session_date": "...", "year": ..., "legislature": "...", "delivery_mode": "..."},
      "agenda_title": "...",
      "text": "<full speech text, may include `## **NAME:**` heading>",
      "text_length_chars": 1612,
      "text_length_words": 247,
      "source_span_chars": [start, end],
      "extraction_path": "pdfs/2024-...extraction.json"
    }
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

# Stratification tiers — slugs match `id` field in src/monitorul_ii/registries/persons.json.
# A politician with 0 codable speeches in the corpus is logged and skipped without
# halting the run; the pilot selection is best-effort across the available tier members.
TIERS: dict[str, list[tuple[str, str]]] = {
    "populist": [
        ("tudor-corneliu-vadim", "Corneliu Vadim Tudor"),
        ("sosoaca-diana", "Diana Iovanovici-Șoșoacă"),
        (
            "simion-george-nicolae",
            "George-Nicolae Simion",
        ),  # registry has duplicate split clusters; this is the one bound to corpus speeches
        ("damureanu-ringo", "Ringo Dămureanu"),
        # ("georgescu-calin", ...),  # presidential candidate, not a parliamentarian; no MO speeches
    ],
    "moderate": [
        ("citu-florin", "Florin Cîțu"),
        ("orban-ludovic", "Ludovic Orban"),
        ("nastase-adrian", "Adrian Năstase"),
    ],
    "register_shift": [
        ("basescu-traian", "Traian Băsescu"),
        ("ponta-victor-viorel", "Victor Ponta"),
        ("popescu-tariceanu-calin", "Călin Popescu-Tăriceanu"),
    ],
}


def _word_count(text: str) -> int:
    return len(text.split())


def _walk_speeches(pdfs_root: Path):
    """Yield (extraction_path, sidecar_dict, agenda_idx, activity_idx, activity_dict).

    Filters to plenary stenogram + plenary joint session sidecars; speech activities
    only (`type == "speech"`). Skips docs that fail to parse rather than halting the
    walk — the pilot tolerates a few corrupt sidecars.
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
    tier: str,
) -> dict:
    speaker = activity.get("speaker") or {}
    metadata = sidecar.get("metadata") or {}
    text = activity.get("text") or ""
    src_span = activity.get("source_span") or {}
    return {
        "speech_id": activity.get("id"),
        "document_id": sidecar.get("document_id"),
        "tier": tier,
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


def select(
    pdfs_root: Path,
    *,
    per_populist: int,
    per_moderate: int,
    per_shifter: int,
    min_words: int,
    max_words: int,
    seed: int,
) -> list[dict]:
    """Walk the corpus once, bucket every eligible speech by (tier, speaker_id),
    then sample deterministically per the seed."""

    targets = {
        "populist": per_populist,
        "moderate": per_moderate,
        "register_shift": per_shifter,
    }
    slug_to_tier = {
        slug: tier for tier, members in TIERS.items() for slug, _ in members
    }

    by_tier_speaker: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    seen_speech_ids: set[str] = set()
    walked = 0

    for path, sidecar, ai, ji, agenda_item, activity in _walk_speeches(pdfs_root):
        walked += 1
        speaker = activity.get("speaker") or {}
        person_id = speaker.get("person_id")
        if person_id is None or person_id not in slug_to_tier:
            continue
        text = activity.get("text") or ""
        words = _word_count(text)
        if not (min_words <= words <= max_words):
            continue
        speech_id = activity.get("id")
        if speech_id is None or speech_id in seen_speech_ids:
            continue
        seen_speech_ids.add(speech_id)
        record = _emit_record(
            extraction_path=path,
            sidecar=sidecar,
            agenda_idx=ai,
            activity_idx=ji,
            agenda_item=agenda_item,
            activity=activity,
            tier=slug_to_tier[person_id],
        )
        by_tier_speaker[slug_to_tier[person_id]][person_id].append(record)

    print(
        f"[info] walked {walked} speech activities; bucketed across tiers:",
        file=sys.stderr,
    )
    for tier, members in TIERS.items():
        for slug, canonical in members:
            count = len(by_tier_speaker.get(tier, {}).get(slug, []))
            tag = "ok" if count > 0 else "MISSING"
            print(
                f"  [{tag}] {tier:15s} {slug:32s} {canonical:35s} {count:4d}",
                file=sys.stderr,
            )

    rng = random.Random(seed)
    picked: list[dict] = []
    for tier, members in TIERS.items():
        target_per = targets[tier]
        for slug, canonical in members:
            speeches = by_tier_speaker.get(tier, {}).get(slug, [])
            if not speeches:
                continue
            rng.shuffle(speeches)
            picked.extend(speeches[:target_per])

    return picked


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=Path("validation/pilot_speeches.jsonl"))
    p.add_argument("--pdfs", type=Path, default=Path("pdfs"))
    p.add_argument("--per-populist", type=int, default=1)
    p.add_argument("--per-moderate", type=int, default=1)
    p.add_argument("--per-shifter", type=int, default=1)
    p.add_argument("--min-words", type=int, default=100)
    p.add_argument("--max-words", type=int, default=800)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk + bucket but do not write the output file.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.pdfs.exists():
        print(f"[error] --pdfs path {args.pdfs} does not exist", file=sys.stderr)
        return 2

    picked = select(
        args.pdfs,
        per_populist=args.per_populist,
        per_moderate=args.per_moderate,
        per_shifter=args.per_shifter,
        min_words=args.min_words,
        max_words=args.max_words,
        seed=args.seed,
    )

    print(f"[info] picked {len(picked)} speeches total:", file=sys.stderr)
    by_tier: dict[str, int] = defaultdict(int)
    for r in picked:
        by_tier[r["tier"]] += 1
    for tier, n in sorted(by_tier.items()):
        print(f"  {tier:15s} {n:3d}", file=sys.stderr)

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
