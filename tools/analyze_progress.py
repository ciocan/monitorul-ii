#!/usr/bin/env python3
"""Summarise an analyze-run JSONL log.

Usage:
    .venv/bin/python tools/analyze_progress.py [LOG_FILE]

If no path is given, picks the most recent `data/analyze-runs/*.jsonl`.

Run alongside (or after) `monitorul-ii analyze ... --log-file ...` to see
cumulative counters: outcomes, voice-skip rate (the regression we just
closed; should now stay near 0%), per-prompt error breakdown, cost / call
throughput, top-failed sidecars. Pair with `watch -n 30` to live-poll.
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path


def _resolve_log(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    candidates = sorted(glob.glob("data/analyze-runs/*.jsonl"))
    if not candidates:
        sys.exit("no analyze-run log found in data/analyze-runs/")
    return Path(candidates[-1])


def _ts_range(records: list[dict]) -> tuple[str, str] | None:
    ts = [r.get("ts") for r in records if r.get("ts")]
    if not ts:
        return None
    return ts[0], ts[-1]


def main(argv: list[str]) -> int:
    log = _resolve_log(argv[1] if len(argv) > 1 else None)
    if not log.exists():
        sys.exit(f"log not found: {log}")

    records: list[dict] = []
    with log.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    n = len(records)
    if n == 0:
        print(f"log: {log}")
        print("(empty — no records logged yet)")
        return 0

    outcomes = Counter(r.get("outcome", "?") for r in records)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in records)
    calls = sum(int(r.get("calls") or 0) for r in records)
    tokens_in = sum(int(r.get("tokens_in") or 0) for r in records)
    tokens_out = sum(int(r.get("tokens_out") or 0) for r in records)

    voice_skip = sum(
        1
        for r in records
        if int(r.get("hawkins_markers") or 0) > 0 and not r.get("voice_ran")
    )
    fallbacks = sum(int(r.get("fallbacks") or 0) for r in records)
    rate_limited = sum(int(r.get("rate_limit_retries") or 0) for r in records)
    repaired = sum(int(r.get("repaired") or 0) for r in records)

    hawkins_pos = sum(1 for r in records if (r.get("hawkins_score") or 0) > 0)
    vparty_pos = sum(1 for r in records if (r.get("vparty_score") or 0) > 0)

    err_kinds: Counter[str] = Counter()
    for r in records:
        for e in r.get("errors") or []:
            err_kinds[e.split(" (", 1)[0]] += 1

    sidecar_fails: Counter[str] = Counter()
    for r in records:
        if r.get("outcome") == "failed":
            sidecar_fails[r.get("sidecar", "?")] += 1

    print(f"log:          {log}")
    print(f"records:      {n}")
    rng = _ts_range(records)
    if rng:
        print(f"ts range:     {rng[0]}  →  {rng[1]}")
    print()
    print("outcomes:")
    for k, v in outcomes.most_common():
        pct = 100.0 * v / n
        print(f"  {k:<10s}  {v:>6d}  ({pct:5.1f}%)")
    print()
    print(
        f"voice_skip_with_markers:  {voice_skip}  ({100 * voice_skip / n:.2f}% of records)"
    )
    print(f"  ↑ the regression we closed — should be ≈0% post-fix")
    print()
    print(f"cost:           ${cost:.4f}")
    print(f"calls:          {calls}  (avg {calls / n:.2f} per record)")
    print(f"tokens in/out:  {tokens_in:,}  /  {tokens_out:,}")
    print(f"hawkins +:      {hawkins_pos}  ({100 * hawkins_pos / n:.1f}% of records)")
    print(f"vparty  +:      {vparty_pos}  ({100 * vparty_pos / n:.1f}% of records)")
    if fallbacks or rate_limited or repaired:
        print()
        print("recovery counters (silent self-heal):")
        print(f"  fallback (json_object):  {fallbacks}")
        print(f"  rate_limit retries:      {rate_limited}")
        print(f"  parse / schema repair:   {repaired}")
    if err_kinds:
        print()
        print("error breakdown (per-prompt):")
        for kind, c in err_kinds.most_common(10):
            print(f"  {c:>4d} × {kind}")
    if sidecar_fails:
        print()
        print(
            f"top failing sidecars (showing {min(5, len(sidecar_fails))} of {len(sidecar_fails)}):"
        )
        for sidecar, c in sidecar_fails.most_common(5):
            print(f"  {c:>3d} × {sidecar}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
