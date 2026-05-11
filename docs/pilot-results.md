# `monitorul-ii pilot-results` — LLM benchmark comparison

## Purpose

Reads a pilot-benchmark results directory (default `data/pilot_results/`) and
produces a ranked comparison of every candidate LLM model against an opus gold
standard across two discourse-analysis axes: **Hawkins populism score** and
**DQI level of justification**.

Read-only; no writes, no network calls, no Elasticsearch dependency.

---

## Directory layout expected

```
data/pilot_results/
  opus/                              ← gold model (one JSON file per speech)
    2023_II_29-agenda-2-act-42.json
    2024_II_105-agenda-6-act-84.json
    ...
  gemini-3.1-flash-lite/             ← candidate model (same filenames)
    2023_II_29-agenda-2-act-42.json
    ...
  gemini-3.1-flash-lite_summary.json ← optional aggregated stats
  gemini-3.1-pro/
    ...
  gemini-3.1-pro_summary.json
  ...
```

**Per-speech JSON** (one file per model per speech):

```json
{
  "speech_id": "mo://2023/II/29#agenda-2#act-42",
  "model_key": "gemini-3.1-flash-lite",
  "model_label": "gemini-3.1-flash-lite",
  "results": {
    "hawkins": {
      "output": { "score": 1, "score_unit": "ordinal_0_2", ... },
      "error": null
    },
    "dqi": {
      "output": { "level_of_justification": 2, ... },
      "error": null
    }
  }
}
```

**Per-model summary JSON** (`<model>_summary.json`):

```json
{
  "n_speeches": 10,
  "totals": {
    "cost_usd": 0.09,
    "latency_ms": 92822,
    "calls": 23
  },
  "errors": {},
  "fragments_not_found_by_kind": { "dqi": 3 }
}
```

The summary is optional; when absent, `cost_usd`, `latency_ms`, and `calls`
default to zero and `n_total` falls back to the number of speech files found.

---

## Usage

```bash
# Default: reads data/pilot_results/, gold = opus, text output
uv run monitorul-ii pilot-results

# Custom directory or gold model
uv run monitorul-ii pilot-results --results-dir /path/to/results --gold opus

# Machine-readable JSON (pipe into jq)
uv run monitorul-ii pilot-results --json | jq '.models[] | {model: .model_label, h: .hawkins.match_rate}'
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--results-dir DIR` | `data/pilot_results` | Root directory containing one subdirectory per model. |
| `--gold MODEL` | `opus` | Subdirectory name of the gold model. All other subdirectories are candidates. |
| `--json` | off | Emit a single JSON object to stdout instead of text tables. |

---

## Output columns

```
+----+----------------------------+----+---------+-------+-----------+---------+---------+--------+--------+-------+
| #  | Model                      | n  | H-match | H-MAD | DQI-match | DQI-MAD | cost/sp | lat/sp | errors | frags |
```

| Column | Source field | Meaning |
|--------|-------------|---------|
| `#` | — | Rank (1 = closest to gold). |
| `Model` | `model_label` from speech file | Verbose model identifier. |
| `n` | file intersection | Speeches present in **both** gold and candidate — the comparison universe. |
| `H-match` | `results.hawkins.output.score` | Exact-match rate: % of speeches where candidate score equals gold score (Hawkins is ordinal 0–2: 0 = no populism, 1 = moderate, 2 = strong). |
| `H-MAD` | same | Mean absolute delta: average \|gold − candidate\| per speech (0.00 = perfect, 2.00 = maximum divergence). |
| `DQI-match` | `results.dqi.output.level_of_justification` | Same exact-match rate for DQI level of justification (ordinal 0–2: 0 = no justification, 1 = inferior, 2 = qualified). |
| `DQI-MAD` | same | Mean absolute delta for DQI. |
| `cost/sp` | `totals.cost_usd / n_speeches` | Estimated cost in USD per speech from the summary file. |
| `lat/sp` | `totals.latency_ms / n_speeches` | Wall-clock latency per speech (includes all four prompt calls: Hawkins → voice → DQI → V-Party). |
| `errors` | sum of `errors{}` values | Total error count across all prompt kinds for this run. |
| `frags` | sum of `fragments_not_found_by_kind{}` | Evidence fragments cited by the model that could not be located back in the speech text. High values indicate the model is hallucinating or paraphrasing evidence rather than quoting verbatim. |

### Ranking order

Models are sorted by:

1. **H-match descending** — highest Hawkins agreement first.
2. **DQI-match descending** — tiebreaker on DQI agreement.
3. **cost/sp ascending** — cheapest as a final tiebreaker for equal agreement.

Models with no overlapping speeches (n = 0, e.g. models that only ran on a
single speech not present in the gold set) sort to the bottom with `n/a`
agreement metrics.

---

## JSON output shape

```json
{
  "generated_at": "2026-05-11T10:37:37+00:00",
  "gold_model": "opus",
  "gold_n_speeches": 10,
  "models": [
    {
      "rank": 1,
      "model_key": "gemini-3.1-flash-lite",
      "model_label": "gemini-3.1-flash-lite",
      "n_paired": 10,
      "n_total": 10,
      "hawkins": {
        "match_rate": 0.9,
        "matches": 9,
        "total": 10,
        "mean_abs_delta": 0.1
      },
      "dqi": {
        "match_rate": 0.9,
        "matches": 9,
        "total": 10,
        "mean_abs_delta": 0.1
      },
      "cost_usd": 0.0,
      "cost_usd_per_speech": 0.0,
      "latency_ms": 92822,
      "latency_ms_per_speech": 9282.2,
      "calls": 23,
      "errors": 0,
      "fragments_missed": 3
    }
  ],
  "notes": [...]
}
```

---

## Implementation

Module: `src/monitorul_ii/pilot.py`  
CLI handler: `cmd_pilot` in `src/monitorul_ii/cli.py`

**Pairing logic**: for each candidate directory, the set of JSON filenames is
intersected with the gold directory. Only files present in both are compared.
Files unique to either side are silently ignored (they cannot be paired).

**Null handling**: if either gold or candidate has `hawkins.output = null` (the
prompt errored), that speech is excluded from the Hawkins pair count but still
counts toward DQI if DQI succeeded. Same logic in reverse. A model with all
errors on one axis will show `n/a` for that axis's match rate.

**Malformed output guard**: some models occasionally emit `dqi.output` as a
JSON array instead of an object (a known schema-drift failure mode). The
extractor checks `isinstance(output, dict)` and skips the pair rather than
crashing.

**Summary fallback**: when no `<model>_summary.json` exists, cost and latency
are zero, and `n_total` equals the count of JSON files in the candidate
directory.

Example output:

== Model ranking vs. opus ==

| #  | Model                      | n  | H-match | H-MAD | DQI-match | DQI-MAD | lat/sp | errors | frags |
|----|----------------------------|----|---------|-------|-----------|---------|--------|--------|-------|
| 1  | gemini-3.1-flash-lite      | 10 | 90.0%   | 0.10  | 90.0%     | 0.10    | 9.3s   | 0      | 3     |
| 2  | gemma-4-26b-a4b-it         | 10 | 90.0%   | 0.10  | 90.0%     | 0.10    | 37.6s  | 0      | 7     |
| 3  | gpt-5.4-mini               | 10 | 90.0%   | 0.10  | 60.0%     | 0.40    | 9.9s   | 0      | 13    |
| 4  | gemma-4-31b-it             | 10 | 80.0%   | 0.20  | 100.0%    | 0.00    | 110.6s | 0      | 3     |
| 5  | deepseek-v4-pro            | 10 | 80.0%   | 0.20  | 66.7%     | 0.33    | 242.1s | 1      | 2     |
| 6  | qwen-3.6-27b               | 10 | 71.4%   | 0.29  | 55.6%     | 0.44    | 184.9s | 4      | 4     |
| 7  | claude-haiku-4.5           | 10 | 70.0%   | 0.30  | 80.0%     | 0.20    | 89.6s  | 0      | 16    |
| 8  | gemini-3.1-pro-preview     | 10 | 66.7%   | 0.33  | 66.7%     | 0.33    | 116.5s | 0      | 0     |
| 9  | claude-sonnet-4.6          | 10 | 60.0%   | 0.40  | 100.0%    | 0.00    | 165.1s | 0      | 2     |
| 10 | deepseek-v4-flash          | 10 | 60.0%   | 0.40  | 90.0%     | 0.10    | 67.5s  | 0      | 7     |
| 11 | qwen-3.6-35b-a3b           | 10 | 60.0%   | 0.40  | 70.0%     | 0.30    | 72.1s  | 0      | 1     |

---

| Column | Source field | Meaning |
|--------|-------------|---------|
| `#` | — | Rank (1 = closest to gold). |
| `H-match` | `results.hawkins.output.score` | Exact-match rate: % of speeches where candidate score equals gold score (Hawkins is ordinal 0–2: 0 = no populism, 1 = moderate, 2 = strong). |
| `H-MAD` | same | Mean absolute delta: average \|gold − candidate\| per speech (0.00 = perfect, 2.00 = maximum divergence). |
| `DQI-match` | `results.dqi.output.level_of_justification` | Same exact-match rate for DQI level of justification (ordinal 0–2: 0 = no justification, 1 = inferior, 2 = qualified). |
| `DQI-MAD` | same | Mean absolute delta for DQI. |
| `lat/sp` | `totals.latency_ms / n_speeches` | Wall-clock latency per speech (includes all four prompt calls: Hawkins → voice → DQI → V-Party). |
| `errors` | sum of `errors{}` values | Total error count across all prompt kinds for this run. |
| `frags` | sum of `fragments_not_found_by_kind{}` | Evidence fragments cited by the model that could not be located back in the speech text. High values indicate the model is hallucinating or paraphrasing evidence rather than quoting verbatim. |
