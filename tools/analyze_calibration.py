"""Summarise a single-model calibration smoke run.

Distinct from `pilot_compare_30.py` (which is a head-to-head between two
models against a gold). This script consumes ONE model's per-speech result
files and emits the distribution / drift / reliability summary used to
confirm calibration holds before committing to corpus-wide indexing.

Inputs:

  - `--results <dir>`     directory containing `<model_key>/*.json` per-speech
                           result files (the pilot_benchmark.py output shape).
  - `--model <key>`       model directory name (default: `gemini-3.1-flash-lite`).
  - `--speeches <jsonl>`  the input sample JSONL (used to read tier/year for
                           per-speech rows the per-result files don't carry).

Outputs (to stdout):

  - Headline distributions (Hawkins, DQI level_of_justification, voice).
  - Year drift table — same distributions broken down by metadata.year,
    surfacing era-specific calibration regressions.
  - Reliability totals (errors, retries, repairs, paraphrase rate).
  - Top speaker / agenda rows where high-populism markers cluster.

Exit codes:

  - 0 on success
  - 1 on missing result dir / unreadable files
  - 2 on bad CLI args
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def load_results(results_dir: Path, model_key: str) -> list[dict[str, Any]]:
    model_dir = results_dir / model_key
    if not model_dir.is_dir():
        print(f"[error] missing dir {model_dir}", file=sys.stderr)
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(model_dir.glob("*.json")):
        if p.name.endswith("_summary.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] {p}: {exc}", file=sys.stderr)
            continue
        out.append(data)
    return out


def hawkins_score(entry: dict[str, Any]) -> int | None:
    out = (entry.get("results") or {}).get("hawkins") or {}
    score = (out.get("output") or {}).get("score")
    return score if isinstance(score, int) else None


def hawkins_confidence(entry: dict[str, Any]) -> float | None:
    out = (entry.get("results") or {}).get("hawkins") or {}
    c = (out.get("output") or {}).get("framework_confidence")
    return c if isinstance(c, (int, float)) else None


def dqi_level(entry: dict[str, Any]) -> int | None:
    out = (entry.get("results") or {}).get("dqi") or {}
    level = (out.get("output") or {}).get("level_of_justification")
    return level if isinstance(level, int) else None


def voice_labels(entry: dict[str, Any]) -> list[str]:
    out = (entry.get("results") or {}).get("voice") or {}
    classifications = (out.get("output") or {}).get("classifications") or []
    labels: list[str] = []
    if isinstance(classifications, list):
        for c in classifications:
            if isinstance(c, dict):
                v = c.get("voice")
                if v:
                    labels.append(v)
    return labels


def metrics(entry: dict[str, Any]) -> dict[str, int]:
    """Sum operational metrics across the (up to 3) calls per speech."""
    totals = {
        "calls": 0,
        "latency_ms": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "errors": 0,
        "retries": 0,
        "repairs": 0,
        "fragments_missed": 0,
        "markers_emitted": 0,
    }
    for kind in ("hawkins", "voice", "dqi"):
        call = (entry.get("results") or {}).get(kind)
        if not call:
            continue
        totals["calls"] += 1
        totals["latency_ms"] += call.get("latency_ms") or 0
        totals["tokens_in"] += call.get("tokens_in") or 0
        totals["tokens_out"] += call.get("tokens_out") or 0
        if call.get("error"):
            totals["errors"] += 1
        totals["retries"] += call.get("retries_used") or 0
        if call.get("parse_repaired"):
            totals["repairs"] += 1
        misses = call.get("fragments_not_found") or []
        totals["fragments_missed"] += len(misses)
        output = call.get("output") or {}
        if not isinstance(output, dict):
            continue
        if kind in ("hawkins", "dqi"):
            markers = output.get("markers") or []
            if isinstance(markers, list):
                totals["markers_emitted"] += len(markers)
        elif kind == "voice":
            classifications = output.get("classifications") or []
            if isinstance(classifications, list):
                totals["markers_emitted"] += len(classifications)
    return totals


def render_year_drift_table(entries: list[dict[str, Any]]) -> list[str]:
    """Per-year breakdown — Hawkins score share + DQI mean + paraphrase rate."""
    by_year: dict[int | None, list[dict]] = defaultdict(list)
    for e in entries:
        year = (e.get("metadata") or {}).get("year")
        by_year[year].append(e)
    lines: list[str] = []
    header = (
        f"{'Year':>5}  {'N':>4}  "
        f"{'H=0':>5}  {'H=1':>5}  {'H=2':>5}  {'H_err':>6}  "
        f"{'D_mean':>7}  {'fp%':>5}  {'errs':>5}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for year in sorted((y for y in by_year if y is not None)):
        ents = by_year[year]
        h = [hawkins_score(e) for e in ents]
        h0 = sum(1 for x in h if x == 0)
        h1 = sum(1 for x in h if x == 1)
        h2 = sum(1 for x in h if x == 2)
        h_err = sum(1 for x in h if x is None)
        d = [dqi_level(e) for e in ents if dqi_level(e) is not None]
        d_mean = statistics.mean(d) if d else 0
        m_total = sum(metrics(e)["markers_emitted"] for e in ents)
        fp_total = sum(metrics(e)["fragments_missed"] for e in ents)
        errs_total = sum(metrics(e)["errors"] for e in ents)
        fp_pct = (fp_total / m_total * 100) if m_total else 0
        lines.append(
            f"{year:>5}  {len(ents):>4}  "
            f"{h0:>5}  {h1:>5}  {h2:>5}  {h_err:>6}  "
            f"{d_mean:>7.2f}  {fp_pct:>4.1f}  {errs_total:>5}"
        )
    return lines


def render_chamber_table(entries: list[dict[str, Any]]) -> list[str]:
    by_chamber: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        chamber = (e.get("metadata") or {}).get("chamber") or "(unknown)"
        by_chamber[chamber].append(e)
    lines: list[str] = []
    header = (
        f"{'Chamber':<18}  {'N':>5}  "
        f"{'H=0%':>6}  {'H=1%':>6}  {'H=2%':>6}  {'D_mean':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for chamber in sorted(by_chamber):
        ents = by_chamber[chamber]
        h = [hawkins_score(e) for e in ents]
        denom = sum(1 for x in h if x is not None)
        h0 = sum(1 for x in h if x == 0) / denom * 100 if denom else 0
        h1 = sum(1 for x in h if x == 1) / denom * 100 if denom else 0
        h2 = sum(1 for x in h if x == 2) / denom * 100 if denom else 0
        d = [dqi_level(e) for e in ents if dqi_level(e) is not None]
        d_mean = statistics.mean(d) if d else 0
        lines.append(
            f"{chamber[:18]:<18}  {len(ents):>5}  "
            f"{h0:>5.1f}  {h1:>5.1f}  {h2:>5.1f}  {d_mean:>7.2f}"
        )
    return lines


def render_top_populist_speakers(
    entries: list[dict[str, Any]], top_n: int = 10
) -> list[str]:
    """Speakers who emit the most Hawkins-non-zero markers in the sample."""
    by_speaker: dict[str, dict[str, Any]] = {}
    for e in entries:
        speaker = e.get("speaker") or {}
        name = speaker.get("name") or speaker.get("raw") or "?"
        score = hawkins_score(e)
        if score is None:
            continue
        agg = by_speaker.setdefault(
            name,
            {"name": name, "id": speaker.get("id"), "n": 0, "h0": 0, "h1": 0, "h2": 0},
        )
        agg["n"] += 1
        agg[f"h{score}"] += 1
    lines: list[str] = []
    header = (
        f"{'Speaker':<35}  {'person_id':<32}  {'N':>3}  {'h0':>3}  {'h1':>3}  {'h2':>3}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    sortable = list(by_speaker.values())
    sortable.sort(key=lambda r: (-(r["h1"] + r["h2"] * 2), -r["n"]))
    for r in sortable[:top_n]:
        if r["h1"] + r["h2"] == 0:
            continue
        lines.append(
            f"{(r['name'] or '?')[:35]:<35}  {(r['id'] or '?')[:32]:<32}  "
            f"{r['n']:>3}  {r['h0']:>3}  {r['h1']:>3}  {r['h2']:>3}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--results", type=Path, default=Path("data/calibration_500"))
    p.add_argument("--model", type=str, default="gemini-3.1-flash-lite")
    p.add_argument(
        "--speeches",
        type=Path,
        default=Path("validation/calibration_500.jsonl"),
        help="Input sample JSONL (used for missing-from-results detection).",
    )
    args = p.parse_args(argv)

    entries = load_results(args.results, args.model)
    if not entries:
        return 1

    speeches = []
    if args.speeches.exists():
        speeches = [
            json.loads(line)
            for line in args.speeches.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    expected_ids = {s["speech_id"] for s in speeches}
    actual_ids = {e["speech_id"] for e in entries}
    missing = sorted(expected_ids - actual_ids)
    extra = sorted(actual_ids - expected_ids)

    print(f"# Calibration smoke summary — {args.model}\n")
    print(f"- Speeches in sample: {len(speeches)}")
    print(f"- Per-speech result files: {len(entries)}")
    if missing:
        print(f"- Missing from results: {len(missing)} (first 5: {missing[:5]})")
    if extra:
        print(f"- Extra in results not in sample: {len(extra)} (first 5: {extra[:5]})")
    print()

    # Headline distributions
    h_hist: Counter[int | str] = Counter()
    h_conf: list[float] = []
    d_hist: Counter[int | str] = Counter()
    voice_hist: Counter[str] = Counter()
    for e in entries:
        h = hawkins_score(e)
        h_hist[h if h is not None else "ERR"] += 1
        c = hawkins_confidence(e)
        if c is not None:
            h_conf.append(c)
        dl = dqi_level(e)
        d_hist[dl if dl is not None else "ERR"] += 1
        for v in voice_labels(e):
            voice_hist[v] += 1

    print("## Hawkins score distribution")
    print()
    n_h = sum(v for k, v in h_hist.items() if k != "ERR")
    for k in [0, 1, 2]:
        v = h_hist.get(k, 0)
        pct = (v / n_h * 100) if n_h else 0
        print(f"- score={k:<2}  {v:>5}  ({pct:>5.1f}%)")
    if h_hist.get("ERR"):
        print(f"- score=ERR  {h_hist['ERR']:>5}")
    if h_conf:
        print(
            f"- framework_confidence mean={statistics.mean(h_conf):.3f}  "
            f"median={statistics.median(h_conf):.3f}  "
            f"min={min(h_conf):.2f}  max={max(h_conf):.2f}"
        )
        below_080 = sum(1 for c in h_conf if c < 0.80)
        print(
            f"- below 0.80 (escalation candidates): {below_080} "
            f"({below_080 / len(h_conf) * 100:.1f}%)"
        )
    print()

    print("## DQI level_of_justification distribution")
    print()
    n_d = sum(v for k, v in d_hist.items() if k != "ERR")
    for k in [0, 1, 2, 3]:
        v = d_hist.get(k, 0)
        pct = (v / n_d * 100) if n_d else 0
        print(f"- level={k:<2}  {v:>5}  ({pct:>5.1f}%)")
    if d_hist.get("ERR"):
        print(f"- level=ERR  {d_hist['ERR']:>5}")
    print()

    print("## Voice classifier distribution")
    print()
    n_v = sum(voice_hist.values())
    for v, count in voice_hist.most_common():
        pct = (count / n_v * 100) if n_v else 0
        print(f"- {v:<28}  {count:>5}  ({pct:>5.1f}%)")
    print()

    # Reliability totals
    tot = Counter()
    for e in entries:
        for k, v in metrics(e).items():
            tot[k] += v
    print("## Reliability + operational totals")
    print()
    print(f"- Total calls           {tot['calls']}")
    print(
        f"- Total latency_ms      {tot['latency_ms']}  ({tot['latency_ms'] / 60000:.1f} min)"
    )
    if tot["calls"]:
        print(f"- Mean latency / call   {tot['latency_ms'] / tot['calls']:.0f} ms")
    print(f"- Tokens in             {tot['tokens_in']:,}")
    print(f"- Tokens out            {tot['tokens_out']:,}")
    print(f"- Errors                {tot['errors']}")
    print(f"- Retries used          {tot['retries']}")
    print(f"- json-repair calls     {tot['repairs']}")
    print(f"- Markers emitted       {tot['markers_emitted']}")
    print(f"- Fragments not found   {tot['fragments_missed']}")
    if tot["markers_emitted"]:
        print(
            f"- Paraphrase rate       "
            f"{tot['fragments_missed'] / tot['markers_emitted'] * 100:.2f}%  "
            f"({tot['fragments_missed']}/{tot['markers_emitted']})"
        )
    print()

    print("## Year drift")
    print()
    print("```")
    for line in render_year_drift_table(entries):
        print(line)
    print("```")
    print()

    print("## Chamber breakdown")
    print()
    print("```")
    for line in render_chamber_table(entries):
        print(line)
    print("```")
    print()

    print("## Top speakers with non-zero Hawkins")
    print()
    print("```")
    for line in render_top_populist_speakers(entries, top_n=15):
        print(line)
    print("```")
    print()

    # Persist
    summary = {
        "model": args.model,
        "n_entries": len(entries),
        "missing_count": len(missing),
        "hawkins_distribution": dict(h_hist),
        "hawkins_confidence_mean": statistics.mean(h_conf) if h_conf else None,
        "hawkins_confidence_median": statistics.median(h_conf) if h_conf else None,
        "hawkins_below_080_count": sum(1 for c in h_conf if c < 0.80) if h_conf else 0,
        "dqi_distribution": dict(d_hist),
        "voice_distribution": dict(voice_hist),
        "operational_totals": dict(tot),
        "paraphrase_rate": (
            tot["fragments_missed"] / tot["markers_emitted"]
            if tot["markers_emitted"]
            else None
        ),
    }
    out_path = args.results / f"_calibration_summary_{args.model}.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[info] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
