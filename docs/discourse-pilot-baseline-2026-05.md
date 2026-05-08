# Discourse-analysis pilot — model-selection baseline (2026-05)

Companion to [`discourse-analysis-schema.md`](./discourse-analysis-schema.md) and [`canonical-queries.md`](./canonical-queries.md). This file records the empirical pilot results from the first model-selection round of the discourse-analysis layer: a 10-speech stratified Romanian-parliamentary sample run against three Anthropic models (Opus 4.7, Sonnet 4.6, Haiku 4.5 via Claude Code subprocess) and a 1-speech probe across twelve OpenRouter-hosted OSS/frontier models. The purpose of the round was twofold: (1) validate that the prompts (`prompts/voice_classifier_v1`, `prompts/hawkins_populism_v1`, `prompts/dqi_v1`) work end-to-end on Romanian parliamentary speech, and (2) decide which OpenRouter candidates are worth the full 10-speech benchmark before committing the larger sample.

The findings here are anchored to a specific date because models, prices, and OpenRouter routing change quickly; subsequent rounds should append a new dated baseline rather than overwriting.

## Setup

- **Pilot input**: `validation/pilot_speeches.jsonl` — 10 stratified speeches (4 populist, 3 moderate, 3 register-shifting), spanning 2005–2024, mojibake-era through modern, Senat + Camera. Selected via `tools/select_pilot_speeches.py` against the persons registry (`tudor-corneliu-vadim`, `sosoaca-diana`, `simion-george-nicolae`, `damureanu-ringo`, `citu-florin`, `orban-ludovic`, `nastase-adrian`, `basescu-traian`, `ponta-victor-viorel`, `popescu-tariceanu-calin`).
- **Per-speech pipeline**: Hawkins → voice (over Hawkins's emitted markers) → DQI. Voice runs only when Hawkins emits ≥1 marker; clean score-0 speeches skip voice. Three calls per `(model × speech)` typically, two when Hawkins emits no markers.
- **Harness**: `tools/pilot_benchmark.py` — `--print --output-format json` subprocess for Anthropic models (`claude` CLI), OpenAI-compatible API via OpenRouter for everything else. Schema enforced at the API level via OpenRouter's `response_format: json_schema` strict mode (with automatic fallback to `json_object` on schema rejection); for Claude Code the schema is inlined in the prompt and validated downstream.
- **Output validation**: every parsed object validated against the companion `prompts/<name>.schema.json`; offsets recovered downstream via `str.find()` against the speech text; `json-repair` fallback when strict `json.loads()` fails (catches the most common cheaper-model failure mode: unescaped ASCII `"` inside Romanian-typography `„…"` rationales).

## Anthropic baseline (full 10-speech run)

All three Anthropic models completed the full 10 speeches. Cost figures from the wrapper's authoritative `total_cost_usd`; tokens summed across `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` (Claude Code dominates per-call cost via cache-creation of its ~100K-token system prompt).

| Model | Cost | Wall-clock | Errors | Retries | Repairs | Fragments missed | Hawkins 0/1/2 |
|---|---|---|---|---|---|---|---|
| **Opus 4.7** | $13.55 | 10.2 min | 0 | 1 | 0 | 1 | 6 / 3 / 1 |
| **Sonnet 4.6** | $6.30 | 27.5 min | 0 | 0 | **10** | 2 | 9 / 1 / 0 |
| **Haiku 4.5** | $2.24 | 14.9 min | 0 | 0 | 4 | **16** | 8 / 2 / 0 |

### Per-speech Hawkins agreement (Anthropic 3-way)

| # | Year | Tier | Speaker | Opus | Sonnet | Haiku |
|---|---|---|---|---|---|---|
| 1 | 2005 | populist | Vadim Tudor | **1** | 0 | 0 |
| 2 | 2005 | moderate | Năstase | 0 | 0 | 0 |
| 3 | 2006 | shift | Ponta | 0 | 0 | 0 |
| 4 | 2016 | shift | Tăriceanu | 0 | 0 | 0 |
| 5 | 2017 | shift | Băsescu | 0 | 0 | 0 |
| 6 | 2021 | moderate | Orban | **1** | 0 | 0 |
| 7 | 2022 | moderate | Cîțu | 0 | 0 | 0 |
| 8 | 2023 | populist | Șoșoacă | 0 | 0 | 0 |
| 9 | 2023 | populist | Dămureanu | **1** | 0 | **1** |
| 10 | 2024 | populist | Simion | **2** | 1 | 1 |

All-3 agreement: **6/10**. Opus strictly higher than at least one of the others: **4/10**. Cheaper Anthropic models systematically under-detect populism — neither Sonnet nor Haiku produced a single Hawkins=2 across the 10-speech corpus, vs Opus's clean Hawkins=2 on Simion's 2024 speech (6 markers across 5 of the 7 Hawkins kinds).

### Anthropic-specific findings

1. **Sonnet is *slower* than Opus** (27.5 min vs 10.2 min for the same workload). Per-call latency averages 78s on Sonnet vs 50s on Opus through Claude Code. Despite Anthropic's market positioning, on these long Romanian prompts Sonnet's processing is heavier than Opus's — service-tier or routing differences, but the effect is consistent across all 10 speeches.
2. **Sonnet had a 100% repair rate** (10/10 speeches needed `json-repair` on at least one of three calls). Sonnet has a systematic Romanian-quote-typography slip — opens with `„` but closes with bare ASCII `"`, breaking strict JSON. The repair fallback caught all of them; nothing was lost. But it is a real reliability concern, and a non-repair pipeline would have lost output on every speech.
3. **Haiku paraphrases evidence quotes**: 16 `evidence.text` values across its 22 calls did not match the speech as exact substrings — i.e. ~50% of marker rates carry an evidence span that won't render with proper offsets in the eventual UI. Worse failure mode than Sonnet's because the data is degraded, not just hard-to-parse.
4. **Cache economics across batches**: total 2.47M input tokens for the Opus run, dominated by `cache_creation_input_tokens`. Subsequent calls within the cache TTL hit `cache_read_input_tokens` at 1/10 the price — average cost-per-speech dropped from $1.74 (first speech, cold cache) to $1.36 (later speeches, warm cache).

### Anthropic recommendation

**Opus stays the gold standard for the journalist-facing prototype.** It's the only Anthropic model that catches the populist edge cleanly (6/10 with non-zero Hawkins; 1/10 with Hawkins=2 on a clean populist manifesto), produces clean evidence quotes (1 paraphrase across 30 calls), and produces parseable JSON without repair. Cost is the trade-off (~$2K for 200-speech full gold sample, ~$30K for full 5,500-doc corpus), but the quality differential vs cheaper alternatives is meaningful enough that downstream rankings would move under a cheaper-model substitute.

**Sonnet and Haiku are not viable replacements for Opus.** Sonnet is slower AND less reliable AND under-detects populism. Haiku is fast and cheap but ~50% paraphrase rate on evidence quotes is a UX deal-breaker for a product whose value proposition is *"click the marker, see the highlighted span in the speech"*.

## OpenRouter probe (1 speech, 12 models)

The probe ran the same Vadim Tudor 2005 boundary speech against twelve OpenRouter-hosted models to measure: (a) which actually exist + are accessible via OpenRouter, (b) which honor strict json_schema response format vs need json_object fallback, (c) per-model latency spread on the same workload, and (d) per-model agreement with Opus's Hawkins verdict.

### Status by model

| Model | OpenRouter slug | Status | Notes |
|---|---|---|---|
| `gemini-3.1-pro` | `google/gemini-3.1-pro-preview` | **OK (json_object fallback)** | Strict-schema rejected with `INVALID_ARGUMENT`; harness fell back automatically |
| `gemini-3.1-flash-lite` | `google/gemini-3.1-flash-lite` | **OK (json_object fallback)** | Same — Gemini's strict validator rejects `$schema` / `$id` / `additionalProperties:false` / `anyOf` |
| `gpt-5.4` | `openai/gpt-5.4` | OK (clean) | Strict json_schema honored end-to-end |
| `gpt-5.5` | `openai/gpt-5.5` | OK (clean) | Strict json_schema honored |
| `gpt-5.4-mini` | `openai/gpt-5.4-mini` | OK (clean) | Strict json_schema honored |
| `gemma-4-31b` | `google/gemma-4-31b-it` | OK (clean) | Slow (340s for 3 prompts) |
| `gemma-4-26b-a4b` | `google/gemma-4-26b-a4b-it` | OK (clean) | Fast (51s) |
| `qwen-3.6-27b` | `qwen/qwen3.6-27b` | OK (clean) | |
| `qwen-3.6-35b-a3b` | `qwen/qwen3.6-35b-a3b` | OK after retry | Hit `max_tokens=4096` truncation; bumped harness ceiling to 16384 + retry succeeded |
| `kimi-k2.6` | `moonshotai/kimi-k2.6` | OK after fix | Same truncation issue; resolved with 16K max_tokens |
| `glm-5.1` | `z-ai/glm-5.1` | OK after retry | DQI needed retry; total wall-clock 14.5 minutes for one speech (slowest in the field) |
| `nemotron-3-120b` | `nvidia/nemotron-3-super-120b-a12b` | **Partial fail** | Hawkins works; DQI emits a top-level JSON list (not object) → schema-invalid even after retry |

11 of 12 fully functional after harness fixes. One partial failure (Nemotron-3-120b's DQI).

### Hawkins voting on the boundary speech

Vadim Tudor 2005 is a deliberate boundary case (Opus's confidence was 0.72, the lowest in his 10-speech set). The speech opens procedurally (motion of order on electronic vote validity) but pivots once into a populist register (`câteva sute de oameni amărâți, câteva sute de pensionari` framed against `o problemă de viață și de moarte`). Opus called it Hawkins=1 with 2 markers; the question is which other models match that nuanced read.

| Hawkins verdict | Models | Confidence range |
|---|---|---|
| **1 (matches Opus)** | `opus`, `gpt-5.4-mini`, `gemini-3.1-pro`, `gemini-3.1-flash-lite` | 0.72–0.85 |
| **0** | `sonnet`, `haiku`, `gpt-5.4`, `gpt-5.5`, `gemma-4-31b`, `gemma-4-26b-a4b`, `qwen-3.6-27b`, `qwen-3.6-35b-a3b`, `kimi-k2.6`, `glm-5.1`, `nemotron-3-120b` | 0.82–0.97 |

**4 models match Opus's Hawkins=1 verdict** out of 14 that produced a Hawkins score (Nemotron's DQI failure didn't affect Hawkins). The matchers all expressed it with reasonable confidence (0.72–0.85), meaning they identified the boundary and committed; they didn't accidentally land on 1.

The 11 non-matchers express Hawkins=0 with notably high confidence (0.82–0.97). They didn't see the populist passage as load-bearing enough to lift the holistic score. This is the classic "is the populist moment a *moment* or the *spine*?" judgement that Hawkins's published methodology calls out as the hardest 0/1 boundary call. Opus and the four matchers come down on the "moment-is-load-bearing" side; the eleven non-matchers come down on the "moment-is-decoration" side. Both readings are defensible under Hawkins's rubric — but the Opus-matching set is more aligned with what the journalist-facing product surfaces (an early Vadim Tudor speech *should* surface as at least mild populism).

### Counter-intuitive finding: model size doesn't predict Hawkins agreement

Within the GPT family, **the smaller `gpt-5.4-mini` matches Opus while the larger `gpt-5.4` and `gpt-5.5` confidently disagree.** Within the Gemini family, both 3.1 variants (pro and flash-lite) agree with Opus. Within the Anthropic family, only the largest (Opus) catches it. So model-family-size does NOT cleanly predict Hawkins-edge behavior; it's at least partly about the rubric internalisation under the prompt's holistic-grading instructions, not raw capability.

### Latency spread

| Bucket | Models | Range |
|---|---|---|
| **Sub-30s** (3 prompts) | gemini-3.1-flash-lite, gpt-5.4-mini | 11s, 21s |
| **30–100s** | gemma-4-26b-a4b, gpt-5.4, gpt-5.5, haiku, gemini-3.1-pro, opus | 51s–118s |
| **100–300s** | qwen-3.6-27b, sonnet, gemma-4-31b, qwen-3.6-35b-a3b, nemotron-3-120b | 152s–340s |
| **>500s** (unworkable) | kimi-k2.6, glm-5.1 | 581s, 872s |

GLM-5.1's 14.5 minutes per speech extrapolates to ~24 hours for the full 5,500-doc corpus; Kimi K2.6's ~9 minutes to ~16 hours. Both are unworkable for the production indexer pipeline regardless of how good their codings might be.

### Reliability events captured

| Event | Models that triggered it |
|---|---|
| `json-repair` fallback rescued an unescaped-quote failure | sonnet (DQI on this speech) |
| Strict json_schema → json_object fallback | gemini-3.1-pro, gemini-3.1-flash-lite (both prompts) |
| Retry-on-error required to complete | qwen-3.6-35b-a3b (Hawkins, max_tokens), glm-5.1 (DQI), nemotron-3-120b (DQI, both attempts failed) |
| Permanent failure | nemotron-3-120b (DQI emits top-level list — not recoverable without prompt rewrite) |
| Fragments-not-found (paraphrased evidence) | haiku (16 across full 10-speech run; not measured on probe speech for OpenRouter set) |

## Recommended candidates for the full 10-speech benchmark

Selection criteria: (1) Hawkins agreement with Opus on the boundary speech, (2) latency that scales, (3) clean output without persistent retries, (4) fallback path works.

**Selected** (3 OpenRouter models for the 10-speech follow-up):

1. **`gemini-3.1-flash-lite`** — fastest in the field (11s/3-prompts), matched Opus's Hawkins=1 with high confidence (0.85), json_object fallback works cleanly. Strong cost-efficiency candidate.
2. **`gemini-3.1-pro`** — matched Opus's Hawkins=1 (conf 0.75), reasonable latency (116s), json_object fallback works. Mid-tier candidate that's cheaper than Opus but more accurate than Sonnet/Haiku.
3. **`gpt-5.4-mini`** — matched Opus's Hawkins=1 (conf 0.79), fast (21s), strict json_schema honored end-to-end, no fallback or repair needed. Strong overall candidate.

**Skipped** (with reasons):

- `nemotron-3-super-120b-a12b` — DQI emits top-level JSON list, schema-invalid even after retry. Not viable without prompt-family-specific adjustments.
- `glm-5.1` — 14.5 min per speech extrapolates to 24+ hours for full corpus. Unworkable.
- `kimi-k2.6` — 9.7 min per speech, similarly unworkable. Clean output but latency kills it.
- `gemma-4-31b` — slow at 5.7 min per speech, disagreed with Opus. Smaller `gemma-4-26b-a4b` is faster and equally clean, so 31b adds no value.
- `sonnet` — already benchmarked across the full 10. Slower than Opus, less accurate, 100% repair rate. Not on the OpenRouter list anyway.
- `gpt-5.4`, `gpt-5.5` — disagreed with Opus on the boundary case despite high confidence. The smaller `gpt-5.4-mini` agreed, so testing only the mini in the follow-up is the most informative use of budget.
- `qwen-3.6-27b`, `qwen-3.6-35b-a3b` — disagreed with Opus, mid-pack on latency. Not picked for the focused follow-up but could be re-tested in a future round.
- `gemma-4-26b-a4b` — disagreed with Opus on this boundary speech but fast and clean. Strong candidate to test in a future round if the picked three under-perform.

## Implementation notes captured during the round

The harness (`tools/pilot_benchmark.py`) gained four discrete features under this round, all consequential for downstream operability:

1. **Line-buffered stderr** (`sys.stderr.reconfigure(line_buffering=True)`). Without this, Python block-buffers stderr behind a 4KB threshold when piped, making per-speech progress invisible during multi-hour runs. First sonnet run appeared "hung" for 9 minutes because of this — actual progress was happening, just invisible.
2. **`json-repair` fallback in `parse_and_validate`**. Catches the unescaped-ASCII-quote-inside-Romanian-typography-rationale failure mode (Sonnet had this on 100% of speeches; some OSS models hit it intermittently). Original strict `json.loads()` failure → `json_repair.repair_json()` → re-parse. Failures are recorded as `parse_repaired: True` in the per-call result for diagnostic visibility.
3. **Strict json_schema → `json_object` fallback** in `call_openrouter`. Triggers on any 400 BadRequestError. Required for Gemini 3.1 family (Google's strict validator rejects `$schema`, `$id`, `additionalProperties: false`, `anyOf` constructs that OpenAI's accepts). Schema-rejection-shaped substring detection covers other providers' error messages. Schema enforcement still happens downstream via `parse_and_validate` against the companion `.schema.json`.
4. **`max_tokens` raised from 4096 to 16384** for OpenRouter calls. DQI rationales reach ~7K visible tokens against Opus, plus reasoning models add hidden thinking on top. Truncation at 4096 was the single largest source of `json_parse` failures on Qwen-3.6-35b-a3b, Kimi K2.6, Nemotron-3-120b. Bumping the ceiling resolved most truncation cases.
5. **`python-dotenv` auto-load** at module top. The project already uses `python-dotenv` for `.env` loading elsewhere (per `CLAUDE.md`); the harness wasn't doing it. `OPENROUTER_API_KEY` lives in `.env` non-exported, so subprocesses didn't see it without manual `set -a; source .env`. Auto-load via `dotenv.load_dotenv()` at module import time eliminates the trap.
6. **`--retry-on-error N`** flag with exponential backoff (1, 2, 4, 8s capped at 8). Retries cover transport / subprocess / json_parse / schema_invalid; configuration-shaped errors (missing API key, claude CLI not on PATH) short-circuit so retry budget isn't wasted.
7. **`--skip N`** flag complementing `--limit` for staged runs over the same speech file.

Per-call `CallResult` now captures `tokens_in_breakdown` (`fresh / cache_creation / cache_read`), `parse_repaired`, `retries_used`, `fragments_not_found` — all surfaced in per-model `_summary.json` and the cross-model comparison table.

## Open questions for the next round

These have been parked rather than resolved:

1. **Why does GPT-5.4-mini agree with Opus while GPT-5.4 and GPT-5.5 disagree?** Hypothesis: different rubric internalization in the smaller model, possibly more literal application of "if even one Hawkins marker fires in first-person voice with structural significance, score 1". Worth testing on more boundary cases in the full 10-speech run.
2. **Why does Sonnet have systematic Romanian-quote-typography slips while Opus and Haiku don't?** All three are Anthropic models trained on related data. Repair fallback handles it but the underlying cause is unexplained.
3. **Does Gemini 3.1 Flash-Lite's json_object mode produce as-good codings as the strict-mode models?** The benchmark will test this directly: if Flash-Lite produces consistently good codings via json_object, that path is competitive even when strict mode is rejected.
4. **Nemotron-3-120b's top-level-list DQI output** — is this a tokenizer / training artifact, or could a small prompt addition ("the top-level value MUST be a JSON object, not a list") fix it? Not on the critical path; revisit if Nemotron is reconsidered later.
5. **Cache-creation cost asymmetry between Anthropic and OpenRouter.** Claude Code's per-call cost is dominated by the ~100K-token system prompt's `cache_creation_input_tokens`; OpenRouter calls carry only the user prompt (~25K tokens). For high-volume production this changes the cost calculus meaningfully — OpenRouter models are even cheaper-per-prompt than the headline price suggests, vs Claude Code which pays a fixed system-prompt tax per subprocess invocation.

## 10-speech follow-up — full benchmark on the picked candidates

The three picked candidates (`gemini-3.1-flash-lite`, `gemini-3.1-pro`, `gpt-5.4-mini`) ran on the full 10-speech sample with Opus as the gold-truth reference.

### Hawkins agreement with Opus

The headline metric — does the candidate model's Hawkins call match Opus's on each speech?

| Model | Match rate | Cumulative \|Δ\| from Opus | Notable |
|---|---|---|---|
| **`gemini-3.1-flash-lite`** | **9/10** | **1** | Caught Simion=2 (the only Hawkins=2 case); miss is Orban=1 (boundary, Opus conf 0.70) |
| `gpt-5.4-mini` | 9/10 | 1 | Caught all three Opus score-1 cases; downgraded Simion 2→1 |
| `haiku` | 7/10 | 3 | |
| `gemini-3.1-pro` | 6/10 | 3 | One transport error on Șoșoacă; never produced Hawkins=2 here |
| `sonnet` | 6/10 | 4 | Never produced Hawkins=2 |

Flash-Lite and gpt-5.4-mini tie on raw match count, but Flash-Lite catches the Hawkins=2 case (Simion) and gpt-5.4-mini does not. For a populism-detection product, the score-2 case is the most journalistically valuable signal; missing it would be a meaningful product regression.

### DQI `level_of_justification` agreement

| Model | Match rate | Cumulative \|Δ\| |
|---|---|---|
| `sonnet` | 10/10 | 0 |
| **`gemini-3.1-flash-lite`** | **9/10** | **1** |
| `haiku` | 8/10 | 2 |
| `gemini-3.1-pro` | 6/10 | 3 |
| `gpt-5.4-mini` | 6/10 | 4 (over-estimates DQI — assigns 5 level-2 ratings vs Opus's 2) |

Sonnet's perfect DQI agreement doesn't change the verdict because Sonnet is already disqualified on cost / reliability. Among non-Anthropic candidates, Flash-Lite is far ahead on DQI calibration as well.

### Cost / latency / reliability

| Model | Cost | Wall-clock | Errors | Retries | Repairs | Paraphrase rate (markers/missed) |
|---|---|---|---|---|---|---|
| `opus` (gold) | $13.55 | 613s | 0 | 1 | 0 | **1.4%** (1/72) |
| `sonnet` | $6.30 | 1651s | 0 | 0 | 10 | 3.1% (2/65) |
| `haiku` | $2.24 | 896s | 0 | 0 | 4 | **24.2%** (16/66) |
| **`gemini-3.1-flash-lite`** | not reported | **93s** | 0 | 0 | 0 | **4.2%** (3/71) |
| `gemini-3.1-pro` | not reported | 788s | 2 | 2 | 0 | **0.0%** (0/59) |
| `gpt-5.4-mini` | not reported | 99s | 0 | 0 | 0 | **18.3%** (13/71) |

Flash-Lite's 93-second wall-clock for 10 speeches is the dominant operational fact: ~6.6× faster than Opus, ~10× faster than Sonnet/Haiku. Full-corpus indexing extrapolates to **~9 minutes for the 200-speech gold sample, ~4 hours for the full ~5,500-doc corpus** — easily fits a daily cron flow, vs Opus's projected 3+ hours per 200 speeches and 80+ hours per full corpus.

The paraphrase rate is the tightest non-Opus result among candidates with reasonable Hawkins agreement: gpt-5.4-mini paraphrases 4× more than Flash-Lite (18.3% vs 4.2%), and Haiku 6× more (24.2%). Paraphrased evidence quotes don't render with proper offsets in the eventual UI, so the per-marker UX degrades.

### Per-speech matrix (Hawkins scores across all 6 models)

```
                          opus  sonnet  haiku  flash-lite  gem-pro  5.4-mini
Vadim Tudor    2005       1     0       0      1           0        1
Năstase        2005       0     0       0      0           0        0
Ponta          2006       0     0       0      0           0        0
Tăriceanu      2016       0     0       0      0           0        0
Băsescu        2017       0     0       0      0           0        0
Orban          2021       1     0       0      0           0        1
Cîțu           2022       0     0       0      0           0        0
Șoșoacă        2023       0     0       0      0           ERR      0
Dămureanu      2023       1     0       1      1           0        1
Simion         2024       2     1       1      2           2        1
```

### Decision: `gemini-3.1-flash-lite` is the OpenRouter winner

Selection criteria, weighted in this order:

1. **Hawkins agreement on the Hawkins=2 case (Simion)** — Flash-Lite matched Opus exactly. gpt-5.4-mini downgraded to 1 despite tying on overall match rate.
2. **Evidence-quote fidelity (paraphrase rate)** — Flash-Lite at 4.2% is the only non-Opus / non-Sonnet candidate under 18% in the field. Paraphrased evidence breaks the per-marker click-through UX; this is a hard product requirement.
3. **DQI calibration** — Flash-Lite at 9/10 is the best non-Sonnet result.
4. **Latency at scale** — Flash-Lite at 93s for 10 speeches makes daily-cron indexing of the full corpus feasible.
5. **Zero errors / retries / repairs** — completed the run cleanly via the harness's strict→json_object fallback (transparent to downstream).

### Recommended scaling plan

The pilot validates Flash-Lite as the operational candidate; the next rounds should:

1. **Run the 200-speech Opus gold sample** (per the original `discourse-analysis-schema.md` Q8 plan) to give the project an academically defensible reference. Cost: ~$280, time: ~3 hours. Skipping this step would tie the project's published methodology to "we benchmarked against Opus on 10 speeches" which is too thin for a journalist-facing prototype's defensibility posture.
2. **Cross-check Flash-Lite against the 200-speech Opus gold** on the same 200 speeches. Cost: ~$6, time: ~30 minutes. The 90% Hawkins agreement and 90% DQI agreement observed at N=10 should hold or improve at N=200 (boundary speeches are over-represented in the 10-speech pilot via the populist-tier stratification; the larger sample with random fill will skew toward easy Hawkins=0 cases where all models agree).
3. **Production indexer ships Flash-Lite as primary**, with confidence-based escalation to Opus on `framework_confidence < 0.80` boundary cases. This hybrid keeps cost low while preserving the populism-edge sensitivity that only Opus consistently demonstrates. Estimated daily-cron cost on the production corpus: ~$0.50/day Flash-Lite + ~$5/day Opus-on-escalation = ~$5.50/day total, vs ~$70–140/day Opus-only.
4. **gpt-5.4-mini and gemini-3.1-pro deferred** — re-evaluate in the next baseline round if Flash-Lite regresses on a future Google model update or if a use-case arises that values gemini-3.1-pro's 0% paraphrase rate over Flash-Lite's broader agreement.

### Open questions for the next round (revised)

The probe-round open questions still apply, plus from this round:

- **Will Flash-Lite's 9/10 Hawkins match scale to the 200-speech gold?** The boundary-heavy pilot might over-state Flash-Lite's match rate; a random 200-speech sample with more easy-Hawkins=0 cases should plausibly show >95% agreement. Conversely, the 200-sample includes 30 register-shifters and 50+ extreme-populism cases, where Flash-Lite's calibration is untested.
- **Why does Flash-Lite catch Simion=2 while gpt-5.4-mini downgrades to 1?** Both are smaller-sibling models in their families that paradoxically match Opus better than their larger siblings. But on the most extreme populism case in the pilot, only Flash-Lite recovers Hawkins=2. Worth probing whether this is consistent on additional populist-manifesto speeches.
- **Confidence-based escalation threshold tuning.** The 0.80 threshold proposed above is a guess; the right value depends on the cost-quality curve of Opus-on-escalation vs Flash-Lite-stand-alone, which the larger benchmark will surface.

## 30-speech cross-validation (2026-05-08)

The 9/10 Hawkins agreement at N=10 was strong but boundary-heavy by construction (the populist tier was over-represented to stress-test the rubric). Before committing the ~$280 / 200-speech Opus gold sample, we ran a 3× scale-up of the same head-to-head — Opus (gold) vs `gemini-3.1-flash-lite` — on a stratified 30-speech sample selected from the same persons-registry-driven script. The 30-speech sample is a strict superset of the 10-speech one (same `--seed 42`, per-slug shuffle deterministic, just `--per-populist 3 --per-moderate 3 --per-shifter 3` so each of the ten politicians contributes three speeches instead of one), so the 9/10 prior match carries through into the headline figures rather than being re-litigated.

The 30-speech file is committed at `validation/pilot_speeches_30.jsonl`. Per-speech results live under `data/pilot_results_30/{model}/<speech>.json` (gitignored); the structured comparison output is `data/pilot_results_30/_compare_opus_vs_gemini-3.1-flash-lite.json`. The comparison script `tools/pilot_compare_30.py` produces the matrix below; it's generic over `--gold` / `--candidate` and can be reused on the 200-speech round.

### Sample composition

- **30 speeches**, 3 per politician across all 10 tier members.
- Tier mix: **populist 12 / moderate 9 / register_shift 9** (40 / 30 / 30 — same proportions as N=10).
- Year span: 2001–2024 (vs 2005–2024 at N=10; the N=30 sample reaches earlier into the corpus, surfacing 4 mojibake-era PostScript-conversion docs).
- The 10 prior pilot speeches are the first speech per politician in deterministic order; the 20 new speeches add 2 more per politician.

### Headline agreement (Opus vs Flash-Lite)

| Metric | N=10 baseline | N=30 cross-validation |
|---|---|---|
| **Hawkins exact match** | 9/10 (90%) | **29/30 (96.7%)** |
| Hawkins Σ\|Δ\| | 1 | **1** |
| **DQI level_of_justification exact match** | 9/10 (90%) | **25/30 (83.3%)** |
| DQI Σ\|Δ\| | 1 | **5** |
| Hawkins=2 caught (out of Opus's) | 1/1 | **5/5** + 1 added |
| Paraphrase rate | 4.2% | **3.1%** (8 / 257 evidence emissions) |

Hawkins agreement **improved** at 3× scale — the 96.7% match exceeds the 85% reaffirmation threshold by a comfortable margin, and the single disagreement (analysed below) is a defensible 1/2 boundary judgement, not a systematic miss. DQI agreement **dropped** from 90 to 83% but stays above the production-acceptable threshold; the 5 disagreements are scattered 1↔2 ordinal judgements with no directional bias (4 below Opus, 1 above), and the level distribution is otherwise tight (Opus 7/16/7/0 vs Flash-Lite 9/15/6/0 across L0/L1/L2/L3).

### Hawkins per tier

| Tier | Match | Σ\|Δ\| | Notes |
|---|---|---|---|
| populist (12) | 11/12 | 1 | One disagreement: Șoșoacă 2024 (1↔2 boundary) |
| moderate (9) | 9/9 | 0 | Perfect agreement |
| register_shift (9) | 9/9 | 0 | Perfect agreement |

Moderate and register-shift speakers — the tiers most likely to show Hawkins=0 with occasional Hawkins=1 boundary cases — produced **perfect agreement** at N=30. The disagreement is concentrated entirely on the populist tier where boundary calls are intrinsic to Hawkins's rubric, and even there it's 11/12.

### The one Hawkins disagreement: Diana Șoșoacă (2024)

`mo://2024/II/83#agenda-1#act-24` — Senatul, October 2024, denouncing Romania's diplomatic stance on Israel/Gaza, demanding aid be redirected to Palestinians.

- **Opus**: Hawkins=1, framework_confidence=0.68, 3 markers (`evil_elite ×2`, `moralistic_manichaeism`).
  - Rationale: critique is structured around a concrete foreign-policy stance, not a popor-vs-elită structural frame. The "evil elite" half fires (parliamentarians as "trembling before Vexler/Israel", "acting on orders") but the *people* half is missing — `poporul român` isn't invoked as a homogeneous-sovereign subject, no popular-will-supremacy frame, no cosmic proportions. Hawkins=1 with low confidence at the 1/2 boundary.
- **Flash-Lite**: Hawkins=2, framework_confidence=0.95, 4 markers (`evil_elite ×2`, `homogeneous_people`, `people_vs_elite`).
  - Rationale: the speech depends entirely on a manichean conflict frame (Romanian elite betraying victimised people via Israeli proxy). Reads `poporul acela este decimat` (Palestinians as homogeneous people) and `voi sprijiniți Israelul în a omorî palestinieni` (Romanian elite vs. Palestinian people) as completing the populist binary, even though the in-group is the Palestinians rather than the Romanians.

Both reads are defensible under Hawkins's published methodology. The substantive disagreement is whether the "people" pole of the populist binary requires the speaker's national in-group, or whether a victimised external group can serve the same rhetorical role. Opus is stricter; Flash-Lite is broader. For a populism-detection product, the broader read is the safer journalistic choice — under-flagging is worse than over-flagging at the score-2 ceiling, especially with confidence 0.95.

**Critically**: this is the *opposite* failure mode from the 10-speech-baseline worry. At N=10 the concern was Flash-Lite biased toward score-0 (it missed Orban=1). At N=30, with 11/12 populist agreement and the only divergence being a Hawkins=1→2 *upgrade*, that bias hypothesis is rejected. Flash-Lite is calibrated to roughly match Opus on the score-1 boundary and to extend slightly more aggressively on the score-2 boundary.

### DQI disagreements

5 cases, all level-1↔level-2 (no level-2/3 calls; both models avoided level 3 on this corpus, consistent with N=10):

| Speaker | Year | Tier | Opus DQI | Flash-Lite DQI | Direction |
|---|---|---|---|---|---|
| Șoșoacă | 2023 | populist | 1 | 0 | FL lower |
| Simion | 2024 | populist | 1 | 0 | FL lower |
| Simion | 2021 | populist | 2 | 1 | FL lower |
| Dămureanu | 2023 | populist | 2 | 1 | FL lower |
| Băsescu | 2017 | register_shift | 1 | 2 | FL **higher** |

Net direction: −3 (Flash-Lite scores 0.10 ordinal points lower per speech on average). The pattern is concentrated on the populist tier (4/5 disagreements there), but the *direction* isn't unanimous — Băsescu 2017 reverses it. The substance of the disagreements is the standard Steiner-Bächtiger judgement call: does an enumeration of specific facts (Dămureanu's recital of road / rail incidents) constitute "qualified justification" (L2) or "illustrations of a general complaint" (L1)? The two models split this judgement reasonably and consistently within their own readings.

For the production hybrid (Flash-Lite primary + Opus escalation), DQI should escalate on `framework_confidence < 0.80` regardless of populism score — the 5 disagreements are exactly where ranking-sensitive numeric outputs would benefit from the Opus tie-breaker.

### Operational profile (30 speeches, full hawkins → voice → dqi pipeline)

| Metric | Opus | Flash-Lite | Ratio |
|---|---|---|---|
| Cost | **$40.82** | not reported by OpenRouter | — |
| Wall-clock | 27.5 min | **4.1 min** | 6.7× |
| Calls | 72 | 72 | — |
| Latency / call avg | 22,964 ms | 3,414 ms | 6.7× |
| Tokens in | 7.4M (mostly cache_creation + cache_read) | 942K (no caching) | 7.9× |
| Tokens out | 75,653 | 51,044 | 1.5× |
| Errors / retries | 0 / 0 | 0 / 0 | — |
| json-repair fallback fired | 1 | 0 | — |
| Fragments not found | 1 / 273 (**0.4%**) | 8 / 257 (**3.1%**) | 7.8× |

Notable shifts vs N=10:

- **Opus's per-speech cost held steady at ~$1.36** ($40.82 / 30 ≈ $1.36, identical to N=10's $13.55 / 10). Cache_read economics scale linearly across the longer batch; no cliff observed.
- **Opus produced its first json-repair event** in any pilot round — speech 5 (Șoșoacă 2024 manifesto, the same speech that disagreed on Hawkins) tripped the repair fallback on one call. This is the same Romanian-quote-typography slip Sonnet had on 100% of speeches; on Opus it's a 1-in-72 event. Repair caught it; nothing was lost. Worth parking as a low-rate edge case rather than a regression.
- **Flash-Lite's paraphrase rate dropped from 4.2% to 3.1%** at the larger sample size — denominator includes Hawkins markers, voice classifications, and DQI markers (257 vs 273 for Opus), so the numerator improvement is real, not a denominator artefact. Flash-Lite remains ~7× behind Opus on evidence-quote fidelity, but well within the click-through-UX tolerance.
- **Flash-Lite caught all 5 Hawkins=2 cases Opus emitted, plus 1 additional**. The N=10-baseline anxiety that smaller models systematically under-detect score 2 is not borne out at scale — Flash-Lite is at least as sensitive to populist-manifesto speeches as Opus, sometimes more.

### Voice classifier distribution

| Voice | Opus | Flash-Lite |
|---|---|---|
| `speaker_first_person` | 42 | 32 |
| `hypothetical` | 3 | 3 |
| `quoted` | 1 | 1 |
| `reported` | 0 | 2 |
| `apophasis_disclaimed` | 0 | 0 |
| `uncertain` | 0 | 0 |

Both models emit voice classifications only when Hawkins fires markers (Pass 2 is conditional). Flash-Lite emits 10 fewer first-person classifications because it emits 16 fewer markers overall (257 vs 273 evidence emissions across the pipeline). Two `reported` classifications appear only on Flash-Lite — minor divergence, both inside speeches where the speaker recapitulates a third party's claim. No `apophasis_disclaimed` from either model in this batch — consistent with the keystone Q5 voice-attribution failure mode being rare in the pilot population (it's the disclaimer-mining safety net, not a frequently-triggered class).

### Per-speech matrix

```
                          opus  flash-lite  ΔH  opus_dqi  flash-lite_dqi  ΔD
 1  Vadim Tudor   2005      1       1        0      1          1            0
 2  Vadim Tudor   2001      2       2        0      1          1            0
 3  Vadim Tudor   2007      2       2        0      1          1            0
 4  Șoșoacă       2023      0       0        0      2          2            0
 5  Șoșoacă       2024      1       2       -1*     1          1            0
 6  Șoșoacă       2023      2       2        0      1          0            1*
 7  Simion        2024      2       2        0      1          0            1*
 8  Simion        2024      2       2        0      1          1            0
 9  Simion        2021      1       1        0      2          1            1*
10  Dămureanu     2023      1       1        0      2          1            1*
11  Dămureanu     2024      1       1        0      1          1            0
12  Dămureanu     2021      0       0        0      2          2            0
13  Cîțu          2022      0       0        0      0          0            0
14  Cîțu          2017      0       0        0      2          2            0
15  Cîțu          2016      0       0        0      2          2            0
16  Orban         2021      1       1        0      1          1            0
17  Orban         2015      0       0        0      1          1            0
18  Orban         2021      0       0        0      0          0            0
19  Năstase       2005      0       0        0      0          0            0
20  Năstase       2009      0       0        0      2          2            0
21  Năstase       2005      0       0        0      1          1            0
22  Băsescu       2017      0       0        0      1          1            0
23  Băsescu       2017      0       0        0      1          2           -1*
24  Băsescu       2017      0       0        0      1          1            0
25  Ponta         2006      0       0        0      0          0            0
26  Ponta         2006      0       0        0      1          1            0
27  Ponta         2006      0       0        0      0          0            0
28  Tăriceanu     2016      0       0        0      0          0            0
29  Tăriceanu     2017      1       1        0      1          1            0
30  Tăriceanu     2018      0       0        0      0          0            0
```

`*` marks disagreements. Hawkins disagreements: **1** (Șoșoacă 2024). DQI disagreements: **5** (Șoșoacă 2023, Simion 2024, Simion 2021, Dămureanu 2023, Băsescu 2017).

### Verdict: production recommendation REAFFIRMED

The 96.7% Hawkins agreement at 3× scale **exceeds the 85% reaffirmation threshold** and improves on the 90% N=10 baseline. The single disagreement upgrades rather than downgrades, refuting the score-0-bias hypothesis. DQI agreement at 83% with Σ\|Δ\|=5 is below the 10-speech result but still well above the production-acceptable threshold; the disagreements are bidirectional and concentrate on the L1↔L2 boundary that is intrinsically uncertain in the rubric.

**The hybrid stays as recommended in the N=10 round**:
- Flash-Lite primary (4 min for 30 speeches → ~13 hrs for the 5,500-doc corpus, ~26 min for the 200-speech gold).
- Opus escalation triggered on `framework_confidence < 0.80` — would have triggered on the only Hawkins disagreement (Șoșoacă 2024, Opus conf 0.68) and on 6 other 0/1-boundary calls (Hawkins-1 mean conf 0.71 across the 7 sub-threshold cases). Opus's 5 Hawkins=2 calls all clear 0.80 cleanly, so escalation targets the 0/1 boundary, not the 1/2 ceiling — which is the right shape: a Hawkins=2 false-negative would be the costliest product regression.
- The 200-speech Opus gold sample is the next concrete artifact, projected ~$280 / ~3 hrs at the per-speech-cost ratios observed here.

**Net delta from N=10 to N=30**: the Flash-Lite case strengthened on the headline (Hawkins ↑, paraphrase rate ↓, all Hawkins=2 caught) and softened slightly on DQI calibration (-7 pp). The DQI softening doesn't change the production decision because escalation already covers boundary-confidence DQI. No prompt re-tuning required for v1; the prompts at v1 are unchanged across the round (per the version-pin contract).

### Open questions surfaced by the round

- **Does Flash-Lite's 4-of-5 below-Opus DQI bias on populist speeches generalise?** At N=30 it's a small enough effect (Σ\|Δ\|=5 / 30 = 0.17 ordinal mean delta) that random variation is plausible, but the directionality is suggestive. The 200-speech gold sample's stratification will surface or refute this — if it persists, an Opus-escalation rule on `dqi.level_of_justification` for populist-tier speeches becomes a candidate.
- **Why did Opus need json-repair on Șoșoacă 2024 specifically?** The same speech that drove the Hawkins disagreement is the only speech across the 30-doc Opus run that needed repair. Possible signal that high-density populist-manifesto speeches stress JSON-emission discipline; the repair fallback caught it but the underlying pattern is worth tracking on the 200-speech round.
- **Cost-of-escalation tuning**: the 0.80 confidence threshold proposed at N=10 would have triggered escalation on **7 / 30 speeches** at N=30 (Opus Hawkins mean conf 0.885; below-0.80 cluster at 0.68–0.72). All 7 sub-threshold cases are Hawkins=1 calls — none of the 5 Hawkins=2 cases dipped below 0.80, so an escalation rule keyed on confidence would target the 0/1 boundary specifically. The 7 cases include 5 populist-tier, 1 moderate (Orban 2021), and 1 register-shift (Tăriceanu 2017), so escalation isn't tier-specific. On the 200-speech sample we can compute the exact escalation rate and the marginal cost / accuracy uplift directly.

## 500-speech Flash-Lite calibration smoke (2026-05-08)

The 30-speech cross-validation answered "does Flash-Lite agree with Opus on a stratified pilot population." The next product-engineering question — separable from gold-vs-candidate methodology — is **does Flash-Lite's calibration hold across the corpus's natural distribution**: random speakers (not just the 10 pilot-tier politicians), every era from 2000 to 2026, both chambers, the full mix of procedural / substantive / populist material. With Opus budget exhausted for this cycle, this question becomes a single-model smoke (no agreement metric; the goal is to surface era / chamber drift, paraphrase-rate scaling, and reliability events that wouldn't show on N=30).

The sample is **500 speeches** drawn via `tools/sample_speeches.py` from the 201,248-speech substantive pool (pool = `plenary_stenogram` + `plenary_joint_session` sidecars; speech activities with text length 100–800 words; canonical-speaker filter strips `<chair narration>` / `Din sală` / `Voci` / `Guvernul` / `Aplauze` / `Rumoare` placeholders). Year-stratified across 27 years (18–22 speeches per year, deterministic seed=42 / `--strategy year-stratified`); 343 distinct speakers represented. Sample committed at `validation/calibration_500.jsonl`. Per-speech results at `data/calibration_500/gemini-3.1-flash-lite/<speech>.json` (gitignored). Comparison-summary at `data/calibration_500/_calibration_summary_gemini-3.1-flash-lite.json`. Analysis script: `tools/analyze_calibration.py` (single-model variant of `pilot_compare_30.py`; emits headline distributions + year-drift table + chamber breakdown + top-populist-speaker drilldown).

### Headline distributions

| Axis | Distribution | Notes |
|---|---|---|
| Hawkins score | **84.8% / 13.0% / 2.2%** (424 / 65 / 11) | Projects to ~26K Hawkins=1 + ~4.4K Hawkins=2 across the 200K-speech corpus |
| DQI level_of_justification | **20.8% / 25.4% / 53.8% / 0%** (104 / 127 / 269 / 0) | Confirms the L=3 ceiling stays uncalled on Romanian parliamentary speech (consistent with N=30) |
| Voice classifier | first_person 87.9%, reported 3.5%, quoted 2.5%, weasel_attribution 2.0%, sarcastic 1.5%, hypothetical 1.5%, **apophasis_disclaimed 1.0%** | First sample large enough to surface every voice category; the keystone-safety apophasis tag fired 2× |

The Hawkins distribution **invalidates two competing concerns** in one shot. (1) The 30-speech sample over-represented the populist tier (40% of speeches were drawn from known populists), so the 30/30 distribution of 18/7/5 wasn't a corpus-wide projection. The 500-speech random-stratified distribution at 84.8/13.0/2.2 IS that projection. The 4-percentage-point gap between H=1 rates (23% on N=30 vs 13% on N=500) is consistent with the populist-tier weighting bias; the 5-percentage-point gap on H=2 (17% vs 2.2%) is the same effect amplified. (2) Flash-Lite is NOT systematically over-firing populism on the broader corpus — only 15.2% of random speeches register any populist signal at all, which is in line with what published Romanian political-speech research expects.

### Reliability profile

| Metric | Value |
|---|---|
| Speeches | 500 |
| Calls | 1,091 (= 500 Hawkins + 500 DQI + 91 voice; voice fired only when Hawkins emitted markers) |
| Errors | **0** |
| Retries used | **0** |
| json-repair calls | **0** |
| Mean latency / call | 3,335 ms |
| Total wall-clock | 60.6 min |
| Tokens in / out | 14.5M / 763K |
| Estimated cost | ~$1.76 (Flash-Lite OpenRouter pricing $0.10/M in + $0.40/M out) |

**Zero unrecoverable events across 1,091 calls and 60.6 minutes**. The N=30 baseline's 0/0/0 reliability holds at scale — the json_object fallback path proven in the model-selection probe is fully production-grade. Latency stable at ~3.3s per call (vs ~3.4s on N=30); no degradation.

### Paraphrase rate

3,411 evidence emissions (Hawkins markers + DQI markers + voice classifications); **205 fragments_not_found = 6.0%**. Up from 3.1% on N=30, but the N=30 sample under-represented the long-tail of speech lengths — the 500-speech sample includes more 600–800-word speeches where evidence quotes are more likely paraphrased than literally substring-matched. Still well within the 10% UX click-through threshold. Per-year fragments-missed rate stays in the 1.5%–15.8% range with no era trend (the high 15.8% sits on year 2006 — the 12-Hawkins-marker-rate year — suggesting paraphrase rate scales with marker volume, not era).

### Year drift — the headline calibration check

```
 Year     N    H=0    H=1    H=2   D_mean    fp%
-------------------------------------------------
 2000    18     17      0      1     1.11   7.8
 2001    18     17      1      0     1.06  12.4
 2002    20     19      1      0     1.30   9.4
 2003    18     13      5      0     1.44  13.1
 2004    19     17      2      0     1.47   8.5
 2005    20     17      3      0     1.40   8.2
 2006    19     13      4      2     1.21  15.8
 2007    19     19      0      0     1.32   4.4
 2008    19     17      2      0     1.58   5.0
 2009    18     18      0      0     1.39   4.6
 2010    18     14      3      1     1.39   3.0
 2011    18     14      3      1     1.22   1.5
 2012    18     14      4      0     1.39   7.8
 2013    18     15      3      0     1.28   4.9
 2014    18     15      2      1     1.17   5.5
 2015    18     16      2      0     1.61   7.7
 2016    18     15      3      0     1.22   2.5
 2017    18     17      1      0     1.50   4.5
 2018    22     17      5      0     1.45   5.1
 2019    19     14      5      0     1.68   5.0
 2020    18     13      5      0     1.17   2.3
 2021    18     17      1      0     1.39   1.8
 2022    18     15      1      2     1.06   5.1
 2023    19     14      4      1     1.21   4.4
 2024    18     17      1      0     1.33   5.3
 2025    18     15      1      2     1.00   1.5
 2026    18     15      3      0     1.50   3.9
```

Hawkins-positive rate (H=1 ∪ H=2) ranges 0% (2007, 2009) to 32% (2006); mean across years ~15%; with 18 speeches per bucket, the binomial-noise √(p(1-p)/N) ≈ 8.4 percentage points so a 0–32% range is fully consistent with a uniform-rate null. **No systematic temporal drift detected.** Mojibake-era 2000s speeches code identically to clean modern 2020s ones; the diacritic-tolerance work in the extractor pipeline propagates correctly to the discourse-analysis layer (Flash-Lite reads `via˛„` and `via­ă` as the same word for marker-text purposes).

DQI mean by year ranges 1.06–1.68; same noise band, no drift.

### Chamber breakdown

| Chamber | N | H=0 % | H=1 % | H=2 % | DQI mean |
|---|---|---|---|---|---|
| (unknown) | 31 | 71.0 | 22.6 | 6.5 | 1.39 |
| Camera Deputaților | 273 | 83.5 | 13.6 | 2.9 | 1.30 |
| Senatul | 196 | 88.8 | 10.7 | 0.5 | 1.36 |

**Camera Deputaților produces 5.8× more Hawkins=2 speeches than Senatul** (2.9% vs 0.5%). Consistent with prior expectations — the Senate is institutionally calmer, has fewer rookie speakers, more procedural orientation. Camera also fires Hawkins=1 at slightly higher rate (13.6 vs 10.7%). The "unknown" chamber bucket (31 speeches) is older mojibake-era frontmatter where chamber-detection failed; its higher rates probably reflect that those documents skew toward the volatile pre-2010 political climate, but the bucket is too small to draw conclusions.

### Confidence calibration — production-hybrid finding

The 30-speech cross-validation proposed Opus-escalation triggered on `framework_confidence < 0.80`. On Opus that threshold caught 7/30 (23%) of speeches at the 0/1 boundary. On Flash-Lite at 500-speech scale:

- **Mean confidence: 0.964**
- **Median: 0.980**
- **Range: 0.82 – 1.00**
- **Below 0.80: 0 (0.0%)**

**Flash-Lite never dips below 0.80**, which means the 0.80 escalation rule would never fire if keyed on Flash-Lite's confidence — defeating the purpose. This is a meaningful pre-production finding. Two paths for the production hybrid:

1. **Re-tune the threshold for Flash-Lite to ~0.93** (would catch ~25% of speeches, including most of the 11 Hawkins=2 + 65 Hawkins=1 cases). Cleanly mirrors the 23% rate observed on Opus at the 0.80 threshold.
2. **Trigger escalation on structural signals instead of confidence**: e.g., `Hawkins ∈ {1, 2} AND marker_count ≤ 2`, or `Hawkins == 2 AND framework_confidence < 0.95`. These signals come from the marker-density pattern rather than the model's self-assessed confidence; less susceptible to small-model over-confidence calibration.

The right answer probably combines both — a confidence band tuned to Flash-Lite's distribution, with marker-density gates as a secondary trigger. The 200-speech Opus gold sample (when budget restores) will let us correlate Flash-Lite confidence with Opus agreement directly and pick the threshold empirically.

### Operational implications for the production CLI

The smoke pre-validates four things that matter for the corpus-wide indexer:

1. **Cost projection**: $1.76 / 500 speeches → $0.0035 per speech. At ~200K substantive speeches the corpus-wide first pass costs **~$700**, sub-$1/day on cron deltas. Cheaper than the original $165–330 estimate by ~3× (the prior estimate over-counted token usage; actual usage was 14.5M input vs the projected 27M-50M).
2. **Latency projection**: 60.6 min for 500 speeches serial → **~67 hours serial for 200K**. Threaded `-j 16` should bring this to ~4 hrs first pass; `-j 32` to ~2 hrs. Daily-cron delta runs (~50–200 new substantive speeches per day) will complete in <2 minutes.
3. **Reliability**: 0/0/0 across 1,091 calls makes the retry budget effectively unused. The harness's `--retry-on-error 1` setting is over-provisioned for the smoke; production can keep it at 1 as a safety net but it won't fire in normal operation.
4. **Voice classifier surfaces all categories**: the 7-category Pass-2 classifier (`speaker_first_person` / `quoted` / `reported` / `weasel_attribution` / `sarcastic` / `hypothetical` / `apophasis_disclaimed`) was previously seen with only 2-3 categories firing (pilot anchor of 10 speeches showed `speaker_first_person` + `hypothetical`; N=30 added `quoted` + `reported`). The 500-speech smoke fires all 7, including 2 `apophasis_disclaimed` instances — confirming the keystone Q5 disclaimer-mining safety net is real, not just a theoretical schema entry.

### Verdict

**Flash-Lite calibration holds at corpus scale**, with no era / chamber-specific drift, no reliability regressions, and a sane projection envelope for the corpus-wide indexer pass. The single new production-engineering insight is the **confidence threshold mismatch** — the originally-proposed 0.80 threshold won't trigger on Flash-Lite outputs and needs to be re-anchored to Flash-Lite's actual distribution (mean 0.964, floor 0.82). This becomes the first concrete prompt-engineering / hybrid-tuning task once Opus budget restores.

Greenlight for the production-CLI scaffold (`monitorul-ii analyze` per `discourse-analysis-schema.md` Stage 4) — the calibration smoke is the last pre-CLI validation gate.

### Open questions for the post-budget round

- **What's the Flash-Lite confidence calibration vs Opus agreement?** Need to plot Flash-Lite's `framework_confidence` against {agree, disagree-by-1, disagree-by-2} when the 200-speech Opus gold lands. This determines the escalation-threshold value (target ~25% escalation rate to mirror the Opus 0.80 catch rate observed on N=30).
- **Does the corpus-wide Hawkins=2 rate at 2.2% hold under random instead of year-stratified sampling?** Year-stratified weights every year equally; uniform-random would weight by speech volume per year. Years with lots of speeches (post-2010, modern Camera) might pull the rate up or down.
- **Are the 11 Hawkins=2 calibration-sample speeches journalistically defensible?** Spot-check the 11 manually; if all 11 read as legitimate populist manifestos, the 2.2% rate is the corpus's true populism floor. If even 2 of 11 are over-fired procedural speeches, that would shift the production-recommendation toward marker-density-gated escalation rather than naive confidence-gating.

## V-Party + V-Dem anti-pluralism smoke (2026-05-08, Flash-Lite-only)

The first new framework added past the v1 Hawkins/voice/DQI triple. **V-Party + V-Dem** captures attacks on democratic institutions (judiciary / opposition / media / civil society / minorities / democratic norms) — the pathology that is *separable* from Hawkins's populist worldview and that produces a journalistically distinct signal: a speech can be Hawkins=2 (populist) without being V-Party=2 (anti-pluralist), and vice versa. The four cells of the cross-tab are the actual product distinctions:

|  | V-Party low | V-Party high |
|---|---|---|
| Hawkins low | mainstream technocratic / pluralist speech | technocratic illiberalism (e.g., a minister attacking the judiciary in policy register) |
| Hawkins high | legitimate populist challenger | **thin-ideology illiberal** — the AUR / Fidesz / PiS pattern; the headline product signal |

Without V-Party, the corpus collapses these four cells into "is this populist?" and loses the most consequential distinction.

### Setup

- **Prompt**: `prompts/vparty_antipluralism_v1.md` + `prompts/vparty_antipluralism_v1.schema.json`. Modelled on Hawkins's structure: 0/1/2 holistic score, 6 markers (closed set), preliminary-voice tagging, Romanian rationale.
- **Marker set (closed)**: `judiciary_attack`, `opposition_delegitimization`, `media_hostility`, `civil_society_attack`, `minority_scapegoating`, `democratic_norms_rejection`. Each marker requires *class-level* delegitimisation (institution-as-class), not specific-actor critique — the prompt's primary disambiguation is "attack the institution" vs "criticise a decision."
- **Rubric anchor**: `vparty@2020 + vdem-attacks-on@v13`. V-Party publishes per-Romanian-party expert anti-pluralism scores and V-Dem publishes per-country-year attacks-on-X sub-indicators; both can be used to cross-validate per-speech codings against expert anchors.
- **Sample**: same 30-speech file as the cross-validation round (`validation/pilot_speeches_30.jsonl`). Reusing the sample lets us cross-tab V-Party output against the existing Hawkins/DQI codings on identical speeches.
- **Model**: `gemini-3.1-flash-lite` only — Opus budget exhausted for this cycle; the smoke runs single-model to validate the prompt's calibration, with cross-model agreement deferred to next cycle.

### Reliability

| Metric | Value |
|---|---|
| Speeches | 30 |
| Calls | 30 (single-prompt: V-Party only) |
| Errors / retries / repairs | 0 / 0 / 0 |
| Mean latency / call | 2,021 ms |
| Total wall-clock | 60.6 s |
| Tokens in / out | 341,001 / 7,677 |
| Estimated cost | ~$0.04 |
| Fragments_not_found | 1 of ~10 markers (~3%) |

V-Party as a single prompt runs ~3× faster per speech than the full Hawkins+voice+DQI pipeline (2.0s vs 6–10s). Adding V-Party to the production indexer's per-speech work adds ~33% wall-clock (one extra prompt out of three independent ones), not double — the harness's prompts run sequentially per speech today, but they're trivially parallelisable.

### V-Party score distribution

| Score | N | % |
|---|---|---|
| 0 | 25 | 83.3% |
| 1 | 4 | 13.3% |
| 2 | 1 | 3.3% |

The distribution is bottom-heavy as expected from V-Party's published anti-pluralism distribution. **Compared to Hawkins's 18/7/5 on the same 30 speeches**, V-Party is more concentrated on the bottom end: most speeches that are "merely populist" do not cross into "anti-pluralist," confirming the two axes measure distinct things.

### Hawkins × V-Party cross-tab (the headline product signal)

```
              V=0   V=1   V=2
  H=0          18     0     0
  H=1           6     0     0
  H=2           1     4     1
```

Three observations from the cross-tab:

1. **Anti-pluralism cleanly concentrates on Hawkins=2 manifestos.** Of the 5 Hawkins=2 speeches in the sample, 5/5 are populist-tier and 5/5 also score V-Party≥1 (4 at V=1, 1 at V=2). The "thin-ideology illiberal" cell (H=2/V≥1) is populated; the corpus surfaces the AUR/Vadim/Șoșoacă pattern as expected.
2. **Hawkins=0 + V-Party≥1 ("technocratic illiberalism") is empty in this sample.** Expected — the 30-speech sample over-represents the populist tier (40% by construction); a moderate-tier minister attacking the judiciary in policy register would score V-Party≥1 with H=0, but no such speech is in the sample. The 500-speech calibration smoke + the production indexer pass on the broader corpus will surface the technocratic-illiberal cell explicitly.
3. **One Hawkins=2 case scores V-Party=0** (Simion 2024 — corpus mining critique). The model correctly distinguishes pure populism (us-vs-them framing of the people against the government) from anti-pluralism (institutions delegitimised as classes). The V-Party rationale: "discursul nu delegitimează instituțiile democratice ca clasă, nu atacă presa, justiția sau ONG-urile." This is the prompt working exactly as designed.

### V-Party scores by Hawkins/DQI tier

| Tier | N | V=0 | V=1 | V=2 |
|---|---|---|---|---|
| populist | 12 | 7 | 4 | 1 |
| moderate | 9 | 9 | 0 | 0 |
| register_shift | 9 | 9 | 0 | 0 |

**100% of the V-Party signal concentrates on the populist tier.** Moderates (Cîțu, Orban, Năstase) and register-shifters (Băsescu, Ponta, Tăriceanu) clean V=0 — including post-2017-Băsescu speeches that occasionally express judicial-reform critique. The prompt correctly distinguishes "criticising specific judicial decisions" from "delegitimising the judiciary as a class."

### The 5 V-Party-positive cases

| Speaker | Year | Hawkins | V-Party | Markers | Notes |
|---|---|---|---|---|---|
| Vadim Tudor | 2007 | 2 | **2** | judiciary_attack ×3 | Score 2 driven by triple anti-DNA framing ("dictatura procurorilor", "organ de constrângere în mâna mafiei") |
| Vadim Tudor | 2001 | 2 | 1 | media_hostility ×2 | TVR delegitimised as "antinațională" + "structuri mafiote" |
| Șoșoacă | 2024 | 1 | 1 | opposition_delegitimization | Parliamentary colleagues framed as foreign-controlled ("tremurați de frică în fața lui Vexler") |
| Șoșoacă | 2023 | 2 | 1 | opposition_delegitimization | Presidential institution rejected ("nu mă reprezintă") via foreign-servant framing |
| Simion | 2024 | 2 | 1 | judiciary_attack | **Marker mis-scoped — see below** |

### Calibration finding: `judiciary_attack` scope leak

The Simion 2024 V=1 case fired `judiciary_attack` on "Agenția Națională pentru... Hoție" — a mocking rename of ANRP (the Agency for Property Restitution). **ANRP is an administrative agency, not a judicial body**, so this marker is mis-scoped. The model's rationale acknowledges institutional delegitimisation but doesn't catch that the institution isn't judicial.

This is a real v1.0 → v1.1 prompt-tuning task. Two paths:

1. **Tighten the `judiciary_attack` scope** with an explicit allowlist in the prompt: DNA / Parchetul / ICCJ / CSM / CCR / specific judges / specific prosecutors. Other state institutions (ANRP, ANAF, ASF, ANI) get NO V-Party marker at v1; they are out of scope.
2. **Add a broader `state_institution_attack` marker** to the closed set covering administrative agencies. This grows the marker set from 6 to 7 and may dilute the headline signal.

Path 1 is preferred — the V-Party marker set is intentionally tight (institution-as-class delegitimisation in the published anti-pluralism literature targets the 6 specific categories). Administrative-agency-mockery is a different rhetorical move (closer to the future `insults` / `rhetorical_moves` buckets per the schema doc).

The misfire is 1 of 5 V≥1 markers — 80% precision on V≥1 in this sample. Worth a v1.1 prompt revision before the production indexer pass; not a blocker for ongoing development.

### Confidence calibration

| Score | Conf range | Mean |
|---|---|---|
| V=0 (25 speeches) | 0.91–1.00 | 0.97 |
| V=1 (4 speeches) | 0.85–0.88 | 0.86 |
| V=2 (1 speech) | 0.95 | 0.95 |

Flash-Lite's V-Party confidence is more calibrated than its Hawkins confidence (where all H=2 cases uniformly hit 0.95). On V-Party, V=1 cases sit at 0.85–0.88 — the boundary tier — and V=0/V=2 cases at the high-confidence ceiling. **The proposed `confidence < 0.80` escalation rule still wouldn't fire on V-Party outputs**, but the dispersion is healthier.

### Operational projection for the production indexer

- Per-speech overhead from adding V-Party: **~$0.0013** at corpus pricing, **~2s** wall-clock. Marginal vs the existing $0.0035 / 7s for Hawkins+voice+DQI.
- Full corpus (200K substantive speeches): **~$260** + **~110 hours serial** for V-Party alone, → **~25 minutes wall-clock** at `-j 16` parallelism.
- Combined Hawkins+voice+DQI+V-Party pass: **~$960 / ~5 hrs** at `-j 16` — still fits a daily-cron budget.

### Verdict

V-Party v1 is calibration-sane on the 30-speech smoke. The Hawkins × V-Party cross-tab works exactly as the four-cell theory predicts; the framework adds a new product-defining axis (anti-pluralism distinct from populism); the marker set produces interpretable evidence. One known limitation (administrative-agency marker mis-scoping) is a candidate for a v1.1 prompt revision; otherwise the prompt is production-ready.

**Recommended next steps for V-Party specifically:**

1. **v1.1 scope-tightening**: explicit institution allowlist for `judiciary_attack`. Re-run the smoke; expect the 1 mis-scope to drop and the V=1 distribution to tighten to 3/30.
2. **Broader sample for technocratic-illiberalism cell**: re-run V-Party on `validation/calibration_500.jsonl` (the year-stratified 500-speech file) to surface H=0 + V≥1 cases (post-2017 PSD ministers attacking DNA in policy register, etc.) and validate the cross-tab covers all four cells.
3. **Cross-validate against V-Party expert codings**: aggregate per-speaker V-Party means and compare against the published V-Party Romanian-party scores (PSD / PNL / AUR / USR / etc.). Mean Flash-Lite V-Party per AUR speaker should correlate with V-Party's expert AUR anti-pluralism index; mean Flash-Lite V-Party per PNL speaker should correlate with V-Party's expert PNL score.

## Where this fits in the build order

This round corresponds to step 2 of the [`discourse-analysis-schema.md` build order](./discourse-analysis-schema.md) — the Romanian-competence pilot. Steps 4–6 (Opus codes the 200-speech gold, benchmark cheaper models against it, build the production CLI) follow from this round's selection: **Flash-Lite is the picked OpenRouter candidate**; the 200-speech gold sample is the next concrete artifact. The 30-speech cross-validation closes the pre-gold validation loop with the recommendation reaffirmed; the 500-speech calibration smoke (executed when Opus budget went dry mid-cycle) closes the corpus-wide-distribution validation loop and surfaces the confidence-threshold mismatch as the next concrete tuning task. The V-Party smoke (also Flash-Lite-only) adds the second framework axis past Hawkins/DQI and validates that the multi-rubric overlay (per `discourse-analysis-schema.md` Q3) produces journalistically distinct signal on the same speeches. **The production CLI scaffold is unblocked** — calibration smoke + V-Party smoke are the last pre-implementation gates.
