#!/usr/bin/env -S uv run python
"""Compare two-model pilot benchmark runs head-to-head and emit the per-speech
matrix the discourse-pilot-baseline-2026-05.md doc consumes.

Defaults align with the 30-speech cross-validation (Opus gold vs
gemini-3.1-flash-lite candidate) but the script is generic — pass any two
model keys that produced result directories under `--results`.

Usage:

    uv run python tools/pilot_compare_30.py \\
        --speeches validation/pilot_speeches_30.jsonl \\
        --results data/pilot_results_30 \\
        --gold opus --candidate gemini-3.1-flash-lite

Outputs:

  - stdout: a markdown-shaped per-speech matrix + agreement headline + the
    disagreement breakdown. Suitable to drop into a baseline-doc section.
  - `<results>/_compare_<gold>_vs_<candidate>.json`: the same numbers in
    structured form, for downstream graphing or test fixtures.

The agreement metric is exact-match rate plus cumulative |Δ| (Hawkins is
ordinal 0/1/2; DQI level_of_justification is ordinal 0/1/2/3). Cumulative
|Δ| is the sum of absolute differences across speeches — `Σ |gold - candidate|`
— and is the same metric used in the 10-speech baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load_run(results_dir: Path, model_key: str) -> dict[str, dict[str, Any]]:
    """Load every per-speech file under `<results_dir>/<model_key>/` keyed by speech_id."""
    model_dir = results_dir / model_key
    if not model_dir.is_dir():
        print(f"[error] missing dir {model_dir}", file=sys.stderr)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for p in sorted(model_dir.glob("*.json")):
        if p.name.endswith("_summary.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] {p}: {exc}", file=sys.stderr)
            continue
        sid = data.get("speech_id")
        if sid:
            out[sid] = data
    return out


def hawkins_score(entry: dict[str, Any]) -> int | None:
    out = (entry.get("results") or {}).get("hawkins") or {}
    score = (out.get("output") or {}).get("score")
    return score if isinstance(score, int) else None


def hawkins_confidence(entry: dict[str, Any]) -> float | None:
    out = (entry.get("results") or {}).get("hawkins") or {}
    conf = (out.get("output") or {}).get("framework_confidence")
    return conf if isinstance(conf, (int, float)) else None


def hawkins_marker_count(entry: dict[str, Any]) -> int:
    out = (entry.get("results") or {}).get("hawkins") or {}
    markers = (out.get("output") or {}).get("markers") or []
    return len(markers) if isinstance(markers, list) else 0


def dqi_level(entry: dict[str, Any]) -> int | None:
    out = (entry.get("results") or {}).get("dqi") or {}
    level = (out.get("output") or {}).get("level_of_justification")
    return level if isinstance(level, int) else None


def voice_distribution(entry: dict[str, Any]) -> Counter[str]:
    """Voice labels emitted by the speech's voice classifier call (if any)."""
    out = (entry.get("results") or {}).get("voice") or {}
    classifications = (out.get("output") or {}).get("classifications") or []
    counter: Counter[str] = Counter()
    if not isinstance(classifications, list):
        return counter
    for c in classifications:
        if isinstance(c, dict):
            v = c.get("voice")
            if v:
                counter[v] += 1
    return counter


def per_call_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    """Sum latency/tokens/repairs/retries across the (up to 3) calls.

    `markers_emitted` is the total count of evidence-bearing items across all
    three calls (Hawkins markers + DQI markers + Voice classifications). This
    is the denominator the baseline doc's "paraphrase rate" uses, since
    `fragments_not_found` is collected across the same three call kinds in
    the harness. Counting only Hawkins markers under-counts the denominator
    by ~7×, which inflates the paraphrase percentage.
    """
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


def speech_label(speech: dict[str, Any]) -> str:
    """Short display label for a speech: speaker last word + year."""
    name = (speech.get("speaker") or {}).get("name") or "?"
    year = (speech.get("metadata") or {}).get("year") or "?"
    last_word = name.split()[-1] if name and name != "?" else name
    return f"{last_word} {year}"


def compute_agreement(
    paired: list[tuple[dict, dict, dict]],
    metric_fn,
    *,
    label: str,
) -> dict[str, Any]:
    """Compute exact-match rate + cumulative |Δ| over paired entries."""
    matches = 0
    diffs: list[int] = []
    nulls = 0
    for _speech, gold_entry, cand_entry in paired:
        g = metric_fn(gold_entry)
        c = metric_fn(cand_entry)
        if g is None or c is None:
            nulls += 1
            continue
        if g == c:
            matches += 1
        diffs.append(abs(g - c))
    n = len(paired) - nulls
    return {
        "metric": label,
        "n_pairs_compared": n,
        "n_pairs_with_null": nulls,
        "matches": matches,
        "exact_match_rate": matches / n if n else None,
        "cumulative_abs_delta": sum(diffs),
        "mean_abs_delta": sum(diffs) / n if n else None,
    }


def render_per_speech_matrix(
    paired: list[tuple[dict, dict, dict]],
    *,
    gold_key: str,
    cand_key: str,
) -> list[str]:
    """Render the per-speech matrix as a list of monospace lines."""
    lines: list[str] = []
    header = (
        f"{'#':>2}  {'Year':>4}  {'Tier':<14}  {'Speaker':<28}  "
        f"{'H_g':>3}  {'H_c':>3}  {'ΔH':>3}  "
        f"{'D_g':>3}  {'D_c':>3}  {'ΔD':>3}  "
        f"{'gMs':>5}  {'cMs':>5}  {'fp_g':>4}  {'fp_c':>4}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for i, (speech, gold, cand) in enumerate(paired, start=1):
        year = (speech.get("metadata") or {}).get("year") or "?"
        tier = speech.get("tier") or "?"
        name = (speech.get("speaker") or {}).get("name") or "?"
        h_g = hawkins_score(gold)
        h_c = hawkins_score(cand)
        d_g = dqi_level(gold)
        d_c = dqi_level(cand)
        h_g_s = "ERR" if h_g is None else str(h_g)
        h_c_s = "ERR" if h_c is None else str(h_c)
        d_g_s = "ERR" if d_g is None else str(d_g)
        d_c_s = "ERR" if d_c is None else str(d_c)
        dh = "" if h_g is None or h_c is None else str(h_g - h_c)
        dd = "" if d_g is None or d_c is None else str(d_g - d_c)
        g_lat = per_call_metrics(gold)["latency_ms"]
        c_lat = per_call_metrics(cand)["latency_ms"]
        g_fp = per_call_metrics(gold)["fragments_missed"]
        c_fp = per_call_metrics(cand)["fragments_missed"]
        marker_h = "*" if dh and dh != "0" else " "
        marker_d = "*" if dd and dd != "0" else " "
        lines.append(
            f"{i:>2}  {year:>4}  {tier:<14}  {name[:28]:<28}  "
            f"{h_g_s:>3}  {h_c_s:>3}  {dh + marker_h:>3}  "
            f"{d_g_s:>3}  {d_c_s:>3}  {dd + marker_d:>3}  "
            f"{g_lat:>5}  {c_lat:>5}  {g_fp:>4}  {c_fp:>4}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--speeches",
        type=Path,
        default=Path("validation/pilot_speeches_30.jsonl"),
        help="JSONL of stratified speeches (default: validation/pilot_speeches_30.jsonl).",
    )
    p.add_argument(
        "--results",
        type=Path,
        default=Path("data/pilot_results_30"),
        help="Results dir containing <model_key>/ subdirs (default: data/pilot_results_30).",
    )
    p.add_argument("--gold", type=str, default="opus", help="Gold-truth model key.")
    p.add_argument(
        "--candidate",
        type=str,
        default="gemini-3.1-flash-lite",
        help="Candidate model key.",
    )
    args = p.parse_args(argv)

    if not args.speeches.exists():
        print(f"[error] {args.speeches} does not exist", file=sys.stderr)
        return 2

    speeches = [
        json.loads(line)
        for line in args.speeches.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    gold_run = load_run(args.results, args.gold)
    cand_run = load_run(args.results, args.candidate)
    if not gold_run:
        print(f"[error] gold run {args.gold} has no result files", file=sys.stderr)
        return 1
    if not cand_run:
        print(
            f"[error] candidate run {args.candidate} has no result files",
            file=sys.stderr,
        )
        return 1

    paired: list[tuple[dict, dict, dict]] = []
    missing_in_gold: list[str] = []
    missing_in_cand: list[str] = []
    for speech in speeches:
        sid = speech["speech_id"]
        g = gold_run.get(sid)
        c = cand_run.get(sid)
        if g is None:
            missing_in_gold.append(sid)
            continue
        if c is None:
            missing_in_cand.append(sid)
            continue
        paired.append((speech, g, c))

    print(
        f"# Pilot 30-speech compare: {args.gold} (gold) vs {args.candidate} (candidate)\n"
    )
    print(f"- Speeches loaded: {len(speeches)}")
    print(f"- Pairs comparable: {len(paired)}")
    if missing_in_gold:
        print(f"- Missing in gold ({args.gold}): {len(missing_in_gold)}")
    if missing_in_cand:
        print(f"- Missing in candidate ({args.candidate}): {len(missing_in_cand)}")
    print()

    # Hawkins agreement (overall + per tier)
    overall_h = compute_agreement(paired, hawkins_score, label="hawkins_score")
    overall_d = compute_agreement(paired, dqi_level, label="dqi_level_of_justification")
    print("## Headline agreement")
    print()
    print(
        f"- Hawkins exact match: {overall_h['matches']}/{overall_h['n_pairs_compared']} "
        f"({(overall_h['exact_match_rate'] or 0) * 100:.1f}%); "
        f"Σ|Δ|={overall_h['cumulative_abs_delta']}"
    )
    print(
        f"- DQI level_of_justification exact match: {overall_d['matches']}/{overall_d['n_pairs_compared']} "
        f"({(overall_d['exact_match_rate'] or 0) * 100:.1f}%); "
        f"Σ|Δ|={overall_d['cumulative_abs_delta']}"
    )
    print()

    # Per-tier Hawkins
    print("## Hawkins per tier")
    print()
    by_tier: dict[str, list[tuple[dict, dict, dict]]] = {}
    for speech, g, c in paired:
        by_tier.setdefault(speech.get("tier", "?"), []).append((speech, g, c))
    for tier in sorted(by_tier.keys()):
        pairs = by_tier[tier]
        agg = compute_agreement(pairs, hawkins_score, label=f"hawkins_{tier}")
        print(
            f"- {tier:<14}  match={agg['matches']}/{agg['n_pairs_compared']}  "
            f"Σ|Δ|={agg['cumulative_abs_delta']}"
        )
    print()

    # Distribution of Hawkins scores per model
    g_hist: Counter[int | str] = Counter(
        hawkins_score(g) if hawkins_score(g) is not None else "ERR"
        for _, g, _ in paired
    )
    c_hist: Counter[int | str] = Counter(
        hawkins_score(c) if hawkins_score(c) is not None else "ERR"
        for _, _, c in paired
    )
    print("## Hawkins score distribution")
    print()
    print(
        f"- {args.gold:<22}  0={g_hist.get(0, 0)}  1={g_hist.get(1, 0)}  2={g_hist.get(2, 0)}  "
        f"err={g_hist.get('ERR', 0)}"
    )
    print(
        f"- {args.candidate:<22}  0={c_hist.get(0, 0)}  1={c_hist.get(1, 0)}  2={c_hist.get(2, 0)}  "
        f"err={c_hist.get('ERR', 0)}"
    )
    print()

    # DQI distribution
    g_dq: Counter[int | str] = Counter(
        dqi_level(g) if dqi_level(g) is not None else "ERR" for _, g, _ in paired
    )
    c_dq: Counter[int | str] = Counter(
        dqi_level(c) if dqi_level(c) is not None else "ERR" for _, _, c in paired
    )
    print("## DQI level_of_justification distribution")
    print()
    print(
        f"- {args.gold:<22}  0={g_dq.get(0, 0)}  1={g_dq.get(1, 0)}  2={g_dq.get(2, 0)}  3={g_dq.get(3, 0)}"
    )
    print(
        f"- {args.candidate:<22}  0={c_dq.get(0, 0)}  1={c_dq.get(1, 0)}  2={c_dq.get(2, 0)}  3={c_dq.get(3, 0)}"
    )
    print()

    # Operational totals
    print("## Operational totals")
    print()
    g_tot = Counter()
    c_tot = Counter()
    for _, g, c in paired:
        for k, v in per_call_metrics(g).items():
            g_tot[k] += v
        for k, v in per_call_metrics(c).items():
            c_tot[k] += v
    for label, key in (
        ("calls", "calls"),
        ("latency_ms", "latency_ms"),
        ("tokens_in", "tokens_in"),
        ("tokens_out", "tokens_out"),
        ("errors", "errors"),
        ("retries", "retries"),
        ("repairs", "repairs"),
        ("markers_emitted", "markers_emitted"),
        ("fragments_missed", "fragments_missed"),
    ):
        print(f"- {label:<20}  {args.gold}={g_tot[key]}  {args.candidate}={c_tot[key]}")
    g_par = g_tot["fragments_missed"] / max(1, g_tot["markers_emitted"])
    c_par = c_tot["fragments_missed"] / max(1, c_tot["markers_emitted"])
    print(
        f"- paraphrase_rate     {args.gold}={g_par * 100:.1f}%"
        f"  {args.candidate}={c_par * 100:.1f}%"
    )
    print()

    # Per-speech matrix
    print("## Per-speech matrix")
    print()
    print(
        "Legend: H=Hawkins score, D=DQI level_of_justification, _g=gold, _c=candidate, "
        "ΔH=H_g-H_c (* = mismatch), ΔD=D_g-D_c, Ms=latency total per speech, fp=fragments_not_found."
    )
    print()
    print("```")
    for line in render_per_speech_matrix(
        paired, gold_key=args.gold, cand_key=args.candidate
    ):
        print(line)
    print("```")
    print()

    # Disagreements highlight
    print("## Hawkins disagreements")
    print()
    disagreements: list[tuple[dict, dict, dict]] = []
    for sp, g, c in paired:
        h_g = hawkins_score(g)
        h_c = hawkins_score(c)
        if h_g is None or h_c is None:
            continue
        if h_g != h_c:
            disagreements.append((sp, g, c))
    if not disagreements:
        print("_None — full Hawkins agreement._")
    else:
        for sp, g, c in disagreements:
            sid = sp["speech_id"]
            year = (sp.get("metadata") or {}).get("year")
            tier = sp.get("tier")
            speaker = (sp.get("speaker") or {}).get("name")
            h_g = hawkins_score(g)
            h_c = hawkins_score(c)
            conf_g = hawkins_confidence(g)
            conf_c = hawkins_confidence(c)
            mark_g = hawkins_marker_count(g)
            mark_c = hawkins_marker_count(c)
            print(
                f"- {speaker} ({year}, {tier}) — "
                f"{args.gold}=Hawkins{h_g} (conf {conf_g}, {mark_g} markers) vs "
                f"{args.candidate}=Hawkins{h_c} (conf {conf_c}, {mark_c} markers)"
            )
            print(f"  - {sid}")
    print()

    print("## DQI disagreements")
    print()
    dqi_disagreements: list[tuple[dict, dict, dict]] = []
    for sp, g, c in paired:
        d_g = dqi_level(g)
        d_c = dqi_level(c)
        if d_g is None or d_c is None:
            continue
        if d_g != d_c:
            dqi_disagreements.append((sp, g, c))
    if not dqi_disagreements:
        print("_None — full DQI agreement._")
    else:
        for sp, g, c in dqi_disagreements:
            year = (sp.get("metadata") or {}).get("year")
            tier = sp.get("tier")
            speaker = (sp.get("speaker") or {}).get("name")
            d_g = dqi_level(g)
            d_c = dqi_level(c)
            print(
                f"- {speaker} ({year}, {tier}) — "
                f"{args.gold}=DQI{d_g} vs {args.candidate}=DQI{d_c}"
            )
    print()

    # Voice distribution head-to-head
    g_voice: Counter[str] = Counter()
    c_voice: Counter[str] = Counter()
    for _, g, c in paired:
        g_voice.update(voice_distribution(g))
        c_voice.update(voice_distribution(c))
    print("## Voice classifier distribution")
    print()
    voices = sorted(set(g_voice) | set(c_voice))
    for v in voices:
        print(
            f"- {v:<28}  {args.gold}={g_voice.get(v, 0)}  {args.candidate}={c_voice.get(v, 0)}"
        )
    print()

    # Persist
    structured = {
        "gold": args.gold,
        "candidate": args.candidate,
        "n_paired": len(paired),
        "missing_in_gold": missing_in_gold,
        "missing_in_candidate": missing_in_cand,
        "hawkins_agreement": overall_h,
        "dqi_agreement": overall_d,
        "hawkins_distribution_gold": dict(g_hist),
        "hawkins_distribution_candidate": dict(c_hist),
        "dqi_distribution_gold": dict(g_dq),
        "dqi_distribution_candidate": dict(c_dq),
        "operational_totals_gold": dict(g_tot),
        "operational_totals_candidate": dict(c_tot),
        "voice_distribution_gold": dict(g_voice),
        "voice_distribution_candidate": dict(c_voice),
        "hawkins_disagreements": [
            {
                "speech_id": sp["speech_id"],
                "year": (sp.get("metadata") or {}).get("year"),
                "tier": sp.get("tier"),
                "speaker": (sp.get("speaker") or {}).get("name"),
                "gold": hawkins_score(g),
                "candidate": hawkins_score(c),
                "gold_confidence": hawkins_confidence(g),
                "candidate_confidence": hawkins_confidence(c),
                "gold_marker_count": hawkins_marker_count(g),
                "candidate_marker_count": hawkins_marker_count(c),
            }
            for sp, g, c in disagreements
        ],
        "dqi_disagreements": [
            {
                "speech_id": sp["speech_id"],
                "year": (sp.get("metadata") or {}).get("year"),
                "tier": sp.get("tier"),
                "speaker": (sp.get("speaker") or {}).get("name"),
                "gold": dqi_level(g),
                "candidate": dqi_level(c),
            }
            for sp, g, c in dqi_disagreements
        ],
    }
    out_path = args.results / f"_compare_{args.gold}_vs_{args.candidate}.json"
    out_path.write_text(
        json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[info] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
