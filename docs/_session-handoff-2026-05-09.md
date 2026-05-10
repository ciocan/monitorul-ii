# Session-handoff prompt — production CLI scaffold for the discourse-analysis layer (2026-05-09)

Copy everything below the divider into a fresh Claude Code session as the first message. The prompt is self-contained but references docs the new session must read; the docs carry the full prior context.

---

## Project context (read these first)

You are continuing work on the **monitorul.ai discourse-analysis layer** — a per-speech LLM-based coding pipeline that classifies Romanian parliamentary speech under published rhetorical-analysis rubrics (Hawkins populism, voice attribution, Steiner-Bächtiger DQI, V-Party + V-Dem anti-pluralism). The pilot phase is complete: prompts at v1 (v2 for V-Party) are calibration-validated; Flash-Lite is the picked OpenRouter candidate; the 4-cell Hawkins × V-Party cross-tab populates as designed.

**Read these docs before acting (in this order):**

1. **`docs/discourse-pilot-baseline-2026-05.md`** — the canonical pilot baseline. Records the 15-model probe, the 10-speech benchmark, the 30-speech Opus-vs-Flash-Lite cross-validation, the 500-speech Flash-Lite calibration smoke (Hawkins+voice+DQI), the V-Party v1 30-speech smoke, and the V-Party v2 + 500-speech calibration. **Section 11 (V-Party v2 + 500-speech calibration) is the most recent — read it carefully**; it documents (a) the four-cell Hawkins × V-Party cross-tab populated end-to-end, (b) reliability and cost numbers from the smokes, (c) one open question (`media_hostility = 0` on 500 speeches).
2. **`docs/discourse-analysis-schema.md`** — the design record for the layer. **Q3 (framework anchoring), Q4 (score representation), Q5 (voice/quote-vs-claim attribution), Q6 (multi-rubric overlay), Q11 (re-coding economics)** are the load-bearing decisions. Q5 is the keystone safety decision and explains why voice tracking is critical.
3. **`docs/elasticsearch-indexing.md`** — the canonical ES projection design. **Q3 (enrichment producer model), Q5 (denormalisation rules), Q6 (idempotency triple), Q7 (orphan-delete), Q11 (mapping evolution via additive `put_mapping`)** are the relevant references for how the new producer integrates with the existing indexer.
4. **`CLAUDE.md`** — the project's agent-facing index. Sections to skim: the `embed` subcommand (your reference pattern), the `monitorul_ii.extraction.enrichments.embedding` module description, the indexer's idempotency triple, the `mo-speeches.json` mapping, the daily-cron flow.

The pilot implementation lives in:

- `prompts/voice_classifier_v1.{md,schema.json}` — voice attribution (Pass 2)
- `prompts/hawkins_populism_v1.{md,schema.json}` — populism scoring (0/1/2 holistic)
- `prompts/dqi_v1.{md,schema.json}` — deliberative quality (six sub-codings)
- `prompts/vparty_antipluralism_v2.{md,schema.json}` — anti-pluralism scoring (0/1/2 holistic; **v2 is the production version**, v1 stays in tree for reproducibility)
- `tools/pilot_benchmark.py` — the smoke harness; calls each prompt, validates JSON output, falls back to json_object on schema rejection. **Reference for the OpenRouter adapter; not the production code path.**
- `validation/pilot_speeches.jsonl` (10), `validation/pilot_speeches_30.jsonl` (30), `validation/calibration_500.jsonl` (500) — pilot inputs; do NOT touch.
- `data/pilot_results_30/` and `data/calibration_500*/` — pilot outputs; gitignored.

The reference enrichment producer to model the new code on:

- `src/monitorul_ii/extraction/enrichments/embedding.py` — BGE-M3 embedding producer (`EMBEDDING_NAMESPACE = "embedding"`, `EMBEDDING_PRODUCER = "bge-m3"`, `EMBEDDING_VERSION = "0_1"`). Read this end-to-end before designing the discourse producer; it sets the conventions you should follow (filename pattern, `text_fingerprint` idempotency, atomic `.part`-rename writes, `<x>_sidecar` / `<x>_all` entry points, version-aware refresh).

## Current task — implement `monitorul-ii analyze`

Build the production CLI subcommand that runs the four-prompt pipeline (Hawkins → voice → DQI → V-Party) over every substantive speech in the corpus, persists results as sibling sidecars, and surfaces them through the existing ES indexer.

**Out of scope** for this session (defer to follow-up):
- Opus-escalation hybrid (no Opus budget this cycle).
- Confidence-threshold tuning.
- v3 prompt revisions (e.g., the `media_hostility = 0` v3 follow-up is a separate task; v2 stays the production prompt).
- The 200-speech Opus gold sample (deferred to next budget cycle).
- Per-speaker / per-party V-Party aggregation against published expert scores (deferred).

## What to build

### 1. `src/monitorul_ii/extraction/enrichments/discourse.py` — new producer module

Mirror `embedding.py` exactly in structure. Module-level constants:

```python
DISCOURSE_NAMESPACE = "discourse"   # filename + indexer dict key
DISCOURSE_PRODUCER = "flash-lite"   # filename + `_meta.producer`
DISCOURSE_VERSION = "0_1"           # filename version (underscore form)
DISCOURSE_VERSION_DOTTED = "0.1"    # `_meta.version`
DISCOURSE_MODEL = "google/gemini-3.1-flash-lite"   # OpenRouter slug
MIN_TEXT_CHARS = 100                # mirrors indexer is_substantive cutoff
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
```

Filename pattern: `<basename>.discourse.flash-lite.v0_1.json` next to each sidecar. The indexer's existing enrichment loader (`monitorul_ii.elasticsearch.enrichments.load_enrichments`, regex `<basename>.<namespace>.<producer>.v<version>.json`) already accepts this shape — no loader changes needed.

Per-record entry shape:

```json
{
  "<record_id>": {
    "text_fingerprint": "<12-char sha256>",
    "_meta": {
      "namespace": "discourse",
      "producer": "flash-lite",
      "version": "0.1",
      "model_id": "google/gemini-3.1-flash-lite",
      "source_sidecar_content_sha": "<the sidecar's content_sha at producer-run time>",
      "indexed_at": "<ISO 8601 UTC>",
      "prompt_versions": {
        "hawkins": "v1",
        "voice": "v1",
        "dqi": "v1",
        "vparty": "v2"
      }
    },
    "hawkins": { /* full output of hawkins_populism_v1, may be null on failure */ },
    "voice": { /* full output of voice_classifier_v1, null when Hawkins emitted no markers */ },
    "dqi": { /* full output of dqi_v1 */ },
    "vparty": { /* full output of vparty_antipluralism_v2 */ }
  }
}
```

Key entry points:

- `analyze_sidecar(sidecar_path, *, openrouter_url=None, force=False, write=True, dry_run=False, retry_on_error=1, client=None) -> AnalyzeResult` — analyses one sidecar, walks every substantive speech, runs the 4-prompt pipeline, merges results into the per-sidecar JSON file (atomic `.part`-rename). Idempotent via `text_fingerprint`: skip when fingerprint matches.
- `analyze_all(sidecar_paths, ...)` — iterator entry point with shared `httpx.Client` for connection reuse.
- `healthcheck(openrouter_url, *, api_key)` — `GET /models` probe to fail fast on auth/network issues.

`AnalyzeResult` dataclass (mirrors `EmbedResult`):
- `action: "analyzed" | "skipped" | "dry-run" | "error"`
- `analyzed: int` — records that were freshly coded
- `reused: int` — records skipped via fingerprint match
- `skipped: int` — records below MIN_TEXT_CHARS or non-canonical speakers
- `errors: list[str]`
- `file_path: Path`

**Substantive-speech filter** (shared with the embedding producer's logic):
- Walks `body.agenda_items[*].activities[*]` where `activity.type == "speech"`.
- Filters: `len(text) >= MIN_TEXT_CHARS` AND canonical-speaker (drop `<chair narration>`, `Din sală`, `Voci`, `Guvernul`, `Aplauze`, `Rumoare`).
- The activity's `id` (e.g. `mo://2024/II/119#agenda-1#act-134`) is the entry key.

**Pipeline order per speech**: Hawkins → voice (conditional on Hawkins markers, like `pilot_benchmark.py`) → DQI → V-Party. Lift the calling pattern from `tools/pilot_benchmark.py:run_pipeline_for_speech` and `tools/pilot_benchmark.py:call_openrouter` (json_object fallback included) into `monitorul_ii/extraction/enrichments/discourse.py`. The harness's `parse_and_validate` function (with json-repair fallback) is the production parsing primitive too — lift it.

**Prompt loading**: prompt + schema files live at `prompts/<name>_v<N>.{md,schema.json}`. Load once at producer init, cache. Production prompt versions are pinned: `hawkins=v1, voice=v1, dqi=v1, vparty=v2`.

**Long-tail handling**: speeches longer than ~6,000 words (max_words filter in pilot was 800) need a strategy. v0.1 ships with the same `--max-words 800` filter used in the pilot — speeches longer than that are SKIPPED with reason `text_too_long`. v0.2 will introduce chunked coding. (Document this clearly in module docstring.)

**Idempotency contract**: same as embedding producer.
- `text_fingerprint` = 12-char sha256 of NFC-normalized + whitespace-collapsed speech text.
- On re-run, fingerprint-matched entries reuse the existing entry verbatim (no API call, no file rewrite).
- Mismatched fingerprints trigger re-coding for that record only.
- `--force` overrides the fingerprint match.
- Schema-version bumps (`schema_version` in the sidecar envelope) → re-extract clobbers the discourse file (the producer is NOT a `extractor_versions` participant; the same Q11 contract as the cross-doc linker).

**Error handling**: per-record errors don't fail the sidecar. Record `_meta.errors[]` per record entry; the AnalyzeResult.errors aggregates across the sidecar.

### 2. `monitorul-ii analyze` CLI subcommand

Add to `src/monitorul_ii/cli.py` mirroring the existing `cmd_embed` handler. Argparse signature:

```
monitorul-ii analyze <path> [<path> ...]
                     [--force]
                     [--dry-run]
                     [--limit N]
                     [--reverse]
                     [--openrouter-url URL]
                     [--retry-on-error N]
                     [--max-words N]
                     [--budget-usd N]
                     [-j N | --workers N]
                     [--bucket NAME | --no-upload]
```

- `<path>`: sidecar file or directory (walked for `*.extraction.json` non-recursively, like `embed`).
- `--force`: override the fingerprint match.
- `--dry-run`: walk + count, no API calls, no writes.
- `--limit N`: process at most N sidecars (after `--reverse` is applied).
- `--reverse`: walk newest→oldest like `embed` / `extract`.
- `--openrouter-url URL`: override the default OpenRouter endpoint (also reads `OPENROUTER_URL` env var).
- `--retry-on-error N`: retry budget per call (default 1, exponential backoff 1→2→4→8s capped at 8).
- `--max-words N`: skip speeches above this many words (default 800; long-tail v0.2 task).
- `--budget-usd N`: soft budget cap in USD. The producer estimates per-call spend from `usage.prompt_tokens × OPENROUTER_FLASH_LITE_INPUT_RATE_USD + usage.completion_tokens × OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD` (rates as module constants near the top of `discourse.py`, defaulting to conservative `$0.10/M in + $0.40/M out`). Once cumulative estimated spend exceeds N, the producer finishes the current sidecar atomically and exits cleanly (exit 0, budget-exhausted message in stderr). Resume by re-running — fingerprint-match skips already-coded records at zero API cost.
- `-j N`: parallelism via `ThreadPoolExecutor`. Default 1 (serial). Threads, not processes — the work is network-bound, GIL-friendly via httpx.
- `--bucket NAME` / `--no-upload`: S3 mirror knobs, same shape as `embed`.

CLI smoke-tests `OPENROUTER_API_KEY` is set, fails fast on missing/invalid auth. Progress reporter in the same `_AnalyzeProgressReporter` style (rich bar on isatty, heartbeat in pipes); a fresh class because each pass label differs.

### 3. ES mapping additions to `mo-speeches.json`

Add the discourse fields to `src/monitorul_ii/elasticsearch/mappings/mo-speeches.json` under `enrichments`:

```json
{
  "enrichments": {
    "properties": {
      "discourse": {
        "properties": {
          "hawkins": {
            "properties": {
              "score": {"type": "byte"},
              "framework_confidence": {"type": "float"},
              "marker_count": {"type": "byte"},
              "marker_kinds": {"type": "keyword"}
            }
          },
          "voice": {
            "properties": {
              "dominant_voice": {"type": "keyword"},
              "voices_seen": {"type": "keyword"}
            }
          },
          "dqi": {
            "properties": {
              "level_of_justification": {"type": "byte"},
              "content_of_justification": {"type": "keyword"},
              "respect_for_groups": {"type": "byte"},
              "respect_for_demands": {"type": "byte"},
              "respect_for_counterarguments": {"type": "byte"},
              "constructive_politics": {"type": "keyword"}
            }
          },
          "vparty": {
            "properties": {
              "score": {"type": "byte"},
              "framework_confidence": {"type": "float"},
              "marker_count": {"type": "byte"},
              "marker_kinds": {"type": "keyword"}
            }
          }
        }
      },
      "discourse_producer": {"type": "keyword"},
      "discourse_text_fingerprint": {"type": "keyword"}
    }
  }
}
```

(Preserve the existing `enrichments.embedding` block unchanged; you're adding a sibling subtree.)

The mapping is **purely additive**; push it via `monitorul-ii es-init --update-mappings` (the operator surface already exists). No new generation needed.

### 4. Denormaliser (`monitorul_ii/elasticsearch/denormalize.py`)

Extend `to_speeches_docs(...)` to populate the new `enrichments.discourse.*` fields when a discourse enrichment is present. Pattern: read `enrichments[record_id]["discourse"]["hawkins"]["score"]` etc. and flatten onto the per-speech ES doc. The enrichment loader (`monitorul_ii.elasticsearch.enrichments.load_enrichments`) already merges multi-key payloads under producer-namespaced keys (per its existing logic) — verify the namespacing matches.

`enrichments.discourse.voice.dominant_voice` is computed: argmax over `voice.classifications[*].voice` (when voice ran). When voice didn't run (Hawkins emitted no markers), set to `null`.

`enrichments.discourse.{hawkins,vparty}.marker_count` is `len(markers)`; `marker_kinds` is the deduplicated list of marker `kind` values.

When the discourse enrichment is missing for a record (the producer hasn't run yet), all `enrichments.discourse.*` fields stay unpopulated — ES is sparse-tolerant (same pattern as `enrichments.embedding`).

### 5. Indexer integration

The existing indexer (`monitorul_ii.elasticsearch.indexer.index_one`) reads `<basename>.*.json` enrichments via the regex pattern. **No code change needed** — the new file matches the regex. Verify by:
- Adding `"flash-lite"` to any explicit producer allowlist if one exists (it doesn't, per current code; the loader is producer-agnostic).
- Confirming the `enrichment_fingerprint` (the keystone idempotency leg) covers the new file by globbing `<basename>.*.json` — it already does.

The indexer's `INDEXER_VERSION` does NOT need to bump. Discourse is a sibling enrichment, not an indexer-coordination change.

### 6. Daily-cron flow update (`tools/catchup.py`)

Add `analyze` between `embed` and `index` per `docs/elasticsearch-indexing.md` § daily-cron. Stage filter (skip / only) should accept `analyze` as a valid name. Pre/post checks: `OPENROUTER_API_KEY` set, `OPENROUTER_URL` reachable, sample sidecar has new `<basename>.discourse.flash-lite.v0_1.json` post-run.

### 7. Tests (mandatory per `CLAUDE.md`)

Match the existing `tests/test_embedding.py` shape:

- `tests/test_discourse.py`:
  - `test_analyze_sidecar_idempotent` — run twice, second is no-op (`reused == n_records`).
  - `test_analyze_sidecar_force_overrides_fingerprint` — `--force` re-codes everything.
  - `test_analyze_sidecar_skips_non_canonical_speakers` — `<chair narration>` etc. don't generate entries.
  - `test_analyze_sidecar_skips_short_speeches` — text < MIN_TEXT_CHARS skipped.
  - `test_analyze_sidecar_max_words_long_tail` — `max_words=N` enforces the long-tail filter.
  - `test_analyze_sidecar_voice_skipped_when_hawkins_empty` — voice call doesn't fire when Hawkins has no markers.
  - `test_analyze_sidecar_persists_atomic` — `.part` rename pattern, no half-written files on crash.
  - `test_analyze_cli_dry_run` — argparse-level test that `--dry-run` short-circuits before any HTTP call.
  - `test_analyze_cli_workers_dispatches_threads` — `-j 4` uses ThreadPoolExecutor.
  - `test_analyze_budget_cap_stops_cleanly` — `--budget-usd 0.001` (very low) makes the producer exit 0 after the first sidecar completes; the stderr message includes "budget exhausted"; the sidecar that was mid-run is fully written or fully untouched (no `.part` leftover, no half-merged JSON entry).
- Mock the OpenRouter client (`monkeypatch` `client.chat.completions.create` to return canned responses); do NOT touch real network.
- Use the existing `tests/conftest.py` patterns; if a fixture is missing, add it sparingly.
- `tests/test_denormalize.py` extension: `test_denormalize_speeches_populates_discourse_fields_when_present` and `test_denormalize_speeches_handles_missing_discourse`.
- `tests/test_es_mappings.py` extension (if it exists): the new `enrichments.discourse.*` fields parse and pass `indices.put_mapping` validation against a synthetic stub.

`uv run pytest` must be green.

### 8. Documentation (mandatory per `CLAUDE.md`)

Three docs MUST update:

- **`README.md`**: new `analyze` subcommand prose paragraph + flags. Same depth as `embed`.
- **`CLAUDE.md`**: append `analyze` subcommand to the Commands list with full signature; add a `discourse.py` summary paragraph parallel to the `embedding.py` paragraph; mention the new ES mapping fields and denormaliser change.
- **`docs/architecture.md`**: deeper mechanics — *why* the four-prompt pipeline runs sequentially per speech (rationale: the voice prompt is conditional on Hawkins; DQI and V-Party are independent but kept sequential at v0.1 for simplicity / per-call rate-limit headroom; v0.2 may parallelise). The 4-cell Hawkins × V-Party design rationale (link to `discourse-pilot-baseline-2026-05.md` § 11). Cost projection numbers from the calibration smokes. Why prompt versions are pinned per-framework rather than per-producer. Why `MAX_WORDS = 800` for v0.1 and what happens at v0.2.

### 9. The historical backfill — target: ~40 months (final acceptance criterion)

**Budget**: $100 cap. **Target window**: March 2023 → present (~40 months, ~19,200 substantive speeches). At conservative Flash-Lite pricing ($0.00493/speech × 4 prompts) this lands at ~$94, leaving ~$6 buffer. At optimistic pricing ($0.00368/speech) it lands at ~$71, in which case the operator may extend the cutoff to January 2022 (~52 months / ~27,000 speeches / ~$99) if budget headroom permits.

**Why this window?** Substantive corpus density supports it:
- 2026 partial: ~2,200 speeches (4 months — Jan recess + Apr partial)
- 2025: ~6,000 speeches
- 2024: ~4,300 speeches (election-year recess pattern)
- 2023: ~7,350 speeches (full year)
- — these four years cover the full Ciucă → Ciolacu → Ciolacu II → 2024-election → current-legislature arc, the cancelled-presidential-election crisis (Călin Georgescu rhetoric, including the Șerban V=2 case from Section 11 of the baseline doc), the entire AUR / SOS-România active period, and most of the recent V-Party-positive material.

**Operator-side stop rules** (the producer has no built-in date filter — use `--reverse` + walk + checkpoint):

1. **First**: dry-run the analysis to count records in the target window:
   ```
   uv run monitorul-ii analyze pdfs/ --dry-run --reverse 2>&1 | grep -E '\b(202[3456])-' | head
   ```
2. **Then**: run live with the full sidecar list reversed (newest first), let it cumulatively burn ~$94 worth of speeches. The producer's per-record `_meta.indexed_at` plus the OpenRouter usage in stderr lets the operator monitor cumulative spend; when cumulative tokens approach the budget, **kill with Ctrl+C** — the producer's atomic-write contract guarantees no half-written sidecar files (any `.part` files left behind can be reaped via `find pdfs/ -name '*.part' -delete`).
3. **Resume cleanly**: re-running the same command will skip every already-coded record via the fingerprint-match path (zero API cost), so a Ctrl+C → fix → resume cycle is safe.

The CLI gains a **`--budget-usd N`** flag in this scaffold that estimates spend per call from `usage.prompt_tokens` × `OPENROUTER_FLASH_LITE_INPUT_RATE_USD` + `usage.completion_tokens` × `OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD` (rates as module constants, defaulting to the conservative bounds). The flag is a soft gate: when cumulative-spend estimate exceeds N, the producer finishes the current sidecar atomically and exits cleanly (exit code 0 with the budget-exhausted message in stderr). The two rate constants live near the top of `discourse.py` so the operator can tune them after seeing actual OpenRouter invoices.

```
uv run monitorul-ii analyze pdfs/ -j 16 --max-words 800 --budget-usd 100 --reverse
```

Expected (conservative):
- ~19,200 substantive speeches × 4 prompts within the budget
- **~$94 cost** at $0.00493/speech (the calibration smoke's per-speech rate × 1.0 because the production pipeline has no extra overhead beyond what was measured)
- **~3 hrs wall-clock at `-j 16`** (extrapolating from the 30 min / 3,447 speeches at `-j 16` projection in the baseline doc)
- ~19,200 entries written into `<basename>.discourse.flash-lite.v0_1.json` files alongside sidecars
- 0 errors / 0 retries / 0 repairs (the calibration smokes had this; production should match)

Expected (optimistic) — if pricing comes in under conservative:
- Up to ~27,000 speeches within $100
- ~$71–99 cost
- ~4.5 hrs wall-clock at `-j 16`
- Reach back to **January 2022** (~52 months back), adding the Ukraine-invasion debates and energy-crisis register

If the operator wants a hard deterministic stop instead of `--budget-usd`, the alternative is to walk a date-filtered subset:

```
# Build a list of sidecar paths newer than 2023-03-01 first
.venv/bin/python -c "
import json, sys
from pathlib import Path
from datetime import date
cutoff = date(2023, 3, 1)
for p in sorted(Path('pdfs').glob('*.extraction.json')):
    try:
        d = json.loads(p.read_text())
        sd = (d.get('metadata') or {}).get('session_date') or (d.get('metadata') or {}).get('published')
        if sd and date.fromisoformat(sd[:10]) >= cutoff:
            print(p)
    except Exception:
        pass
" > /tmp/post_2023_sidecars.txt
xargs -a /tmp/post_2023_sidecars.txt uv run monitorul-ii analyze -j 16 --max-words 800
```

After the run completes (or is Ctrl+C'd at the budget cap), `monitorul-ii index pdfs/ --force` projects the new enrichments onto `mo-speeches`. Spot-check via:

```
uv run monitorul-ii query --name search_speeches \
    --params '{"q": "DNA", "filter": {"enrichments.discourse.vparty.score": 2}}'
```

…to verify the new fields are queryable. The Șerban 2025 V=2 case (the only V=2 in the 500-speech calibration smoke) should surface; if it doesn't, the indexer denormaliser is wrong.

Other useful spot-checks once the backfill lands:

```
# Count Hawkins=2 speeches by year
uv run monitorul-ii query --name agg_speeches_by_party_year \
    --params '{"filter": {"enrichments.discourse.hawkins.score": 2}}'

# List the 4 thin-ideology illiberal cases (H=2 + V≥1)
uv run monitorul-ii query --name search_speeches \
    --params '{"filter": {"enrichments.discourse.hawkins.score": 2, "enrichments.discourse.vparty.score": 1}}'
```

## Implementation gotchas (encoded in the smoke harness; lift them into the producer)

- **`uv` on snap buffers stdout** when not piped to a tty. The CLI is invoked via `monitorul-ii analyze ...` so this won't bite end-users, but during development you may need `.venv/bin/python -m monitorul_ii ...` to see output. (The pilot harness ran into this; see `tools/pilot_benchmark.py` comments.)
- **`OPENROUTER_API_KEY`** in `.env`, non-exported. The package already auto-loads `.env` via `python-dotenv` in `cli.py`'s top-of-file pattern; verify and reuse.
- **Strict `json_schema` → `json_object` fallback** is mandatory. The Gemini family rejects strict schema validation (rejects `$schema` / `$id` / `additionalProperties: false` / `anyOf`). Lift the fallback logic from `tools/pilot_benchmark.py:call_openrouter`.
- **`max_tokens=16384`** for OpenRouter calls. DQI rationales reach ~7K visible tokens; reasoning models add hidden thinking. Truncation at 4096 is the largest single source of `json_parse` failures.
- **`json-repair` fallback** in `parse_and_validate`. Catches the unescaped-ASCII-quote-inside-Romanian-typography-rationale failure mode (Sonnet had this on 100% of speeches in the pilot; some OSS models hit it intermittently). Lift from the harness.
- **Char offsets are NOT stored downstream** — prompts emit `evidence.text` only; the `text_fingerprint` lookup reconstructs offsets at query time via `str.find()` against the speech text. This means: never store offsets in the discourse sidecar; instead, downstream consumers (the search layer, the journalist UI) reconstruct them from the persisted speech text + the fingerprint-keyed entry. This matches how the embedding producer handles vectors.
- **Voice runs only when Hawkins has markers.** Lift the conditional from `tools/pilot_benchmark.py:run_pipeline_for_speech`.
- **Pinned prompt versions**: hawkins=v1, voice=v1, dqi=v1, vparty=**v2**. v1 of V-Party is in tree but is NOT the production prompt. The producer's prompt-version stamp must reflect this.

## Acceptance criteria

- `uv run monitorul-ii analyze --help` shows the new subcommand with all flags above.
- `tests/test_discourse.py` exists with at least the 9 tests listed; `uv run pytest` is green.
- `uv run ruff format` and `uv run ruff check --fix` are clean.
- One sample sidecar processed end-to-end: `<basename>.discourse.flash-lite.v0_1.json` exists alongside, has at least one entry, fingerprints round-trip on re-run.
- ES mapping update applied via `monitorul-ii es-init --update-mappings` succeeds (no put_mapping errors).
- Denormaliser populates the new fields on a sample sidecar; a `monitorul-ii index pdfs/<sample>.extraction.json` round-trip verifies the fields land in `mo-speeches`.
- `monitorul-ii query --name search_speeches --params '{"q": "test"}'` includes the new fields in the response (or returns null when discourse hasn't run for that record).
- README.md, CLAUDE.md, docs/architecture.md all reference the new subcommand + mechanics; `grep -n analyze README.md CLAUDE.md docs/architecture.md` shows hits in all three.
- Optional but recommended: the **historical backfill** (~40 months / ~19,200 speeches / ~$94 / ~3 hrs at `-j 16`, with `--budget-usd 100` enforcing the cap) is run as the final acceptance smoke. The CLI must exit cleanly at the budget cap and a re-run must be a no-op skip on every already-coded record.

## Don't-touch list

- **Prompts at v1 / v2** are version-pinned. Do not edit `vparty_antipluralism_v1.md`, `vparty_antipluralism_v2.md`, `hawkins_populism_v1.md`, `voice_classifier_v1.md`, `dqi_v1.md` in place. If you find a calibration flaw, document it as a v3 follow-up and proceed; do NOT block the CLI scaffold on prompt revisions.
- **`validation/pilot_speeches.jsonl`**, **`validation/pilot_speeches_30.jsonl`**, **`validation/calibration_500.jsonl`** — the pilot inputs. Leave as-is.
- **`tools/pilot_benchmark.py`** — the smoke harness. Read it for reference but don't move its logic into the package; instead, lift the relevant primitives (`call_openrouter`, `parse_and_validate`, `find_text_offsets`, the voice-conditional pipeline order) into the new `discourse.py`. The harness itself stays as the discrete smoke tool — useful for future prompt-version A/B tests.
- **`docs/discourse-pilot-baseline-2026-05.md`** — the canonical pilot record. Do not edit; only reference. Production results (the 6-month backfill, post-CLI metrics) belong in a NEW `docs/discourse-production-baseline-2026-05.md` if you want to write them up.
- **The harness's MODELS registry** — only the 6 validated models (opus, sonnet, haiku, gemini-3.1-flash-lite, gemini-3.1-pro, gpt-5.4-mini) should ever be used in benchmarks. The production CLI pins to `gemini-3.1-flash-lite`; do NOT add a `--model` flag at v0.1 (deferred to v0.2 with the Opus-escalation hybrid).

## Cost / time budget

- Implementation work: ~6–10 hrs (producer module + CLI + denormaliser + tests + docs).
- **Historical backfill, $100 budget cap**: targets ~40 months (March 2023 → present), ~19,200 substantive speeches × 4 prompts. Conservative cost ~$94 / optimistic ~$71. Wall-clock ~3 hrs at `-j 16`.
- **Stretch goal if Flash-Lite pricing is cheaper than expected**: extend cutoff to January 2022 (~52 months / ~27,000 speeches / ~$71–99 — still under the $100 cap on optimistic pricing). Wall-clock ~4.5 hrs at `-j 16`.
- **Daily-cron going forward**: ~$0.10/day.

The `--budget-usd 100` flag is the operator-level gate that enforces the cap regardless of which pricing tier OpenRouter actually charges. The producer exits cleanly when the cumulative estimate hits the cap, and resume is a no-op if pricing comes in cheaper than expected.

## Where this fits in the larger build order

This task corresponds to **Stage 4 of the discourse-analysis-schema build order** — the production CLI scaffold. Stages 5–6 (200-speech Opus gold sample; benchmark cheaper models against it; production indexer with Flash-Lite primary + Opus escalation hybrid) follow once Opus budget restores. Stage 7 (Audience-Signals MVP — Tier E zero-LLM ranking) is parallel work that can ship independently.

After this task is complete, the discourse-analysis layer will be **production-ready end-to-end** for Hawkins / voice / DQI / V-Party at corpus scale. The remaining gaps (Opus escalation, additional frameworks per `discourse-analysis-schema.md` Q3 — CMP / CHES / securitization / custom buckets) are independently shippable as additional prompts following the same pattern.

---

End of handoff prompt. Paste everything between the dividers as the first message in a new Claude Code session.
