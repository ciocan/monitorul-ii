# Session-handoff prompt — 30-speech cross-validation (2026-05-08)

Copy everything below the divider into a fresh Claude Code session as the first message. This prompt is self-contained but references three docs the new session should read before acting; the docs carry the full prior context.

---

## Project context (read these first)

You are continuing work on the **monitorul.ai discourse-analysis pilot** — a layer that codes Romanian parliamentary speeches under published rhetorical-analysis rubrics (Hawkins populism, Steiner-Bächtiger DQI, voice attribution) for a journalist-facing prototype. The project's status and decision history is in three docs (read in this order):

1. **`docs/discourse-pilot-baseline-2026-05.md`** — the most recent baseline. Records the 15-model probe, the 10-speech benchmark, and the decision picking `gemini-3.1-flash-lite` as the OpenRouter winner. Includes the per-speech Hawkins matrix and the production-scaling recommendation. **This is the most important doc to read.**
2. **`docs/discourse-analysis-schema.md`** — design doc for the layer (per-speech codings, voice attribution as the keystone failure mode, multi-rubric overlay, build order). Section "Q5 (voice/quote-vs-claim attribution)" is the keystone safety decision.
3. **`docs/canonical-queries.md`** — the version-pinned query catalogue. Captures the inclusion thresholds, voice filtering, and disclaimer discipline that downstream rankings inherit.

The implementation lives in:

- `prompts/voice_classifier_v1.md` + `voice_classifier_v1.schema.json` — voice attribution (the keystone Pass-2 classifier)
- `prompts/hawkins_populism_v1.md` + `hawkins_populism_v1.schema.json` — populism scoring (0/1/2 holistic)
- `prompts/dqi_v1.md` + `dqi_v1.schema.json` — deliberative quality (six sub-codings, the only positive axis)
- `tools/pilot_benchmark.py` — the harness that runs (model × prompt × speech) end-to-end, validates outputs, repairs malformed JSON, falls back from strict json_schema to json_object, captures per-call telemetry
- `tools/select_pilot_speeches.py` — selects stratified speeches from `pdfs/*.extraction.json` against `src/monitorul_ii/registries/persons.json`
- `validation/pilot_speeches.jsonl` — the 10-speech anchor used in the prior baseline (committed to repo)
- `data/pilot_results/{model}/<speech>.json` — per-speech result files from prior runs (gitignored)

## Current task

The 10-speech baseline produced 9/10 Hawkins agreement between Opus (gold) and `gemini-3.1-flash-lite`. We want to confirm this agreement holds at **3× scale (30 speeches)** before committing to the full 200-speech gold sample (which will cost ~$280 in Opus calls and is the academic-defensibility anchor for the project).

Run head-to-head:

- **Opus** via Claude Code subprocess on 30 stratified speeches
- **gemini-3.1-flash-lite** via OpenRouter on the same 30 speeches
- Compute Hawkins agreement, DQI agreement, paraphrase rate, latency
- Compare against the 10-speech baseline; either reaffirm or revise the production recommendation

## Steps

### 1. Extend the pilot sample to 30 speeches

`tools/select_pilot_speeches.py` already implements stratified selection (4 tiers: populist, moderate, register-shift) with deterministic seeding. Either:

- Add a `--per-populist`, `--per-moderate`, `--per-shifter` override (the flags already exist) and run with new values:
  ```
  uv run python tools/select_pilot_speeches.py \
      --out validation/pilot_speeches_30.jsonl \
      --per-populist 4 --per-moderate 3 --per-shifter 3 \
      --seed 42
  ```
  This gives `4 × n_populist + 3 × n_moderate + 3 × n_shifter` speeches; with the current tier definitions (4/3/3 politicians) you get `4×4 + 3×3 + 3×3 = 16 + 9 + 9 = 34` speeches. Adjust per-tier counts to land at exactly 30 if needed.

- Or extend the `TIERS` dict in the script with additional politicians per tier (e.g., add `marga-andrei`, `roman-petre` to populist; `dancila-viorica` to moderate; `iliescu-ion` to register-shift) for richer stratification across more decades. Use the persons registry's high-frequency speakers as the population (`tools/aggregate_speakers.py` produces `data/speaker_clusters.jsonl` with cluster counts).

Sanity-check the output: `wc -l validation/pilot_speeches_30.jsonl` should equal 30.

### 2. Run Opus on the 30 speeches

```
uv run --script tools/pilot_benchmark.py \
    --speeches validation/pilot_speeches_30.jsonl \
    --models opus \
    --retry-on-error 1 \
    --out data/pilot_results_30 | cat
```

Cost expectation: ~$1.36/speech × 30 = **~$45**. Wall-clock: ~10 min/speech × 30 = **~30 min** (subsequent calls hit cache_read so they're cheaper / faster than the first).

### 3. Run gemini-3.1-flash-lite on the same 30 speeches

```
uv run --script tools/pilot_benchmark.py \
    --speeches validation/pilot_speeches_30.jsonl \
    --models gemini-3.1-flash-lite \
    --retry-on-error 1 \
    --out data/pilot_results_30 | cat
```

Cost expectation: low (Flash-Lite is in OpenRouter's cheapest tier). Wall-clock: ~9.3s/speech × 30 = **~5 min**.

`OPENROUTER_API_KEY` is in `.env` (the harness auto-loads via python-dotenv). The Gemini 3.1 family rejects strict json_schema; the harness automatically falls back to `json_object` mode (you'll see `[fallback]` lines in stderr — that is expected, not an error).

### 4. Analyze the 30-speech agreement

Build a comparison script (or extend the analysis from `docs/discourse-pilot-baseline-2026-05.md` § "Per-speech matrix") that computes:

- **Hawkins agreement** with Opus: exact match rate, cumulative |Δ| from Opus
- **DQI level_of_justification agreement** with Opus: same metrics
- **Voice classifier output** distribution (when Hawkins emits markers)
- **Paraphrase rate** (`fragments_not_found / total_markers`)
- **Latency** total + per-call avg
- **Errors / retries / repairs** per model

Per-speech grid (30 rows × 2 models) showing each model's Hawkins and DQI calls. Highlight disagreements.

### 5. Append the 30-speech section to the baseline doc

Append a new section to `docs/discourse-pilot-baseline-2026-05.md` titled `## 30-speech cross-validation (2026-05-DD)`. Include the per-speech matrix, the agreement-at-scale verdict, and either reaffirm or revise the production recommendation. If Flash-Lite's Hawkins agreement holds at >85% on N=30, the recommendation stands and the next step is the full 200-speech Opus gold sample. If it drops below ~80%, the recommendation needs to be revisited (e.g., is Flash-Lite biased toward score-0? does the disagreement cluster on a specific speech type?).

## Implementation gotchas (encoded in the harness; listed for awareness)

- **`OPENROUTER_API_KEY`** in `.env`, non-exported. Harness auto-loads via `dotenv.load_dotenv()`.
- **`uv` on snap buffers stdout** when not piped to a tty. Always pipe through `| cat` to see live output.
- **Stderr is line-buffered** (`sys.stderr.reconfigure(line_buffering=True)` at main entry); progress lines flush as they happen.
- **`json-repair` fallback** in `parse_and_validate` catches Sonnet's Romanian-quote-typography slips and similar malformed outputs from cheaper models.
- **Strict json_schema → json_object fallback** in `call_openrouter` triggers automatically on `BadRequestError`. Gemini 3.1 family always falls back. Schema enforcement still happens downstream.
- **`max_tokens=16384`** for OpenRouter calls (avoids DQI truncation; reasoning models add hidden thinking on top of visible output).
- **Char offsets recovered downstream** — prompts emit only `evidence.text`; the harness `str.find()`s each fragment in the speech text. Paraphrased fragments are recorded as `fragments_not_found` (a quality signal).

## Acceptance criteria

- `validation/pilot_speeches_30.jsonl` exists, 30 lines, stratified.
- 30 speeches × 2 models complete cleanly (or any errors are recorded with explanations).
- `docs/discourse-pilot-baseline-2026-05.md` has a new `## 30-speech cross-validation` section with the per-speech matrix and verdict.
- Production recommendation either reaffirmed (Flash-Lite primary + Opus escalation hybrid) or revised with new reasoning.

## Don't-touch list

- **Prompts and schemas at v1** are version-pinned. If you find a calibration flaw, document it but do NOT edit `v1` files in place; create `v2` in a new file with a `Last modified` date and explain the change in the prompt's `## Versioning` section.
- **`validation/pilot_speeches.jsonl`** (the 10-speech anchor) — leave as-is; the 30-speech file is a separate artifact.
- **The harness's MODELS registry** — only the 6 models with validated wiring (opus, sonnet, haiku, gemini-3.1-flash-lite, gemini-3.1-pro, gpt-5.4-mini) should be used in benchmark runs. Other registered models (gemma-4-*, qwen-3.6-*, kimi-k2.6, glm-5.1, nemotron-3-120b, gpt-5.4, gpt-5.5) had probe-round issues and are kept for future reference but should not be re-benchmarked without re-probing first.

## Cost / time budget

- Total expected cost: **~$47** (Opus ~$45 + Flash-Lite ~$2)
- Total expected time: **~35–40 min wall-clock** (Opus ~30 min + Flash-Lite ~5 min, run sequentially or in parallel)
- This is a one-shot batch — if you find issues mid-run, kill the background task (process IDs visible via `ps -ef | grep pilot_benchmark`), fix the harness, then re-launch from `--skip` of where you left off.

## Where this fits in the larger build order

This task is the final pre-production validation step before committing to:

- The **200-speech Opus gold sample** (per `discourse-analysis-schema.md` Q8 — academically citable κ-style anchor)
- The **`monitorul-ii analyze` CLI scaffold** (Stage 4 in the discourse-analysis schema build order)
- **Audience-signals MVP** (Tier E zero-LLM ranking, ships first per build order)
- **Production indexer with Flash-Lite primary + Opus escalation hybrid**

If the 30-speech cross-validation surfaces a regression in Flash-Lite's agreement, fix the regression (typically a prompt-tuning issue, NOT a model issue) before continuing.
---

End of handoff prompt. Paste everything between the dividers as the first message in a new Claude Code session.
