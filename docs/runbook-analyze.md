# Discourse-analysis runbook

Step-by-step operational guide for running `monitorul-ii analyze` — the four-prompt LLM pipeline (Hawkins → voice → DQI → V-Party) over the corpus. Covers launching, observing, surviving Ctrl+C, recovering from failure modes, and tuning concurrency.

> Sister docs:
> - `docs/discourse-and-semantic-search.md` — what the discourse fields are used for at query time.
> - `docs/discourse-pilot-baseline-2026-05.md` — calibration sweeps and model selection.
> - `docs/architecture.md` § *Discourse-analysis producer* — implementation mechanics.
> - `docs/runbook-catchup.md` — the broader pipeline runbook; analyze sits between embed and index.

---

## 0. Pre-flight

The producer supports two LLM provider backends — pick one per run via `--provider`:

| Provider | Required env vars | Optional env vars | Endpoint | Pricing |
|---|---|---|---|---|
| **OpenRouter** (default) | `OPENROUTER_API_KEY` | `OPENROUTER_URL` (default `https://openrouter.ai/api/v1`) | OpenAI-compatible `/chat/completions` | $0.10 in / $0.40 out per M tokens at Flash-Lite (gateway markup) |
| **Google AI Studio** | `GOOGLE_AI_STUDIO_API_KEY` | `GOOGLE_AI_STUDIO_API_URL` (default `https://generativelanguage.googleapis.com/v1beta`) | Google's native `generateContent` | $0.075 in / $0.30 out per M tokens at Flash-Lite (~25% cheaper, no gateway markup) |

Both route to the same Gemini Flash-Lite model and produce **identical** `<basename>.discourse.flash-lite.v0_1.json` outputs — the indexer's enrichment loader / denormaliser are agnostic to which provider coded the speech. Trade-offs: OpenRouter exposes per-call cost-reporting metadata in the response (the producer sums these into the run's `cost_usd` total); Google AI Studio doesn't, so the producer estimates cost from the per-token rates above. Pick Google when budget matters more than cost-precision telemetry.

```sh
cd /home/ciocan/projects/monitorul
uv sync

# OpenRouter path (default)
grep -E '^OPENROUTER_(API_KEY|URL)=' .env | sed 's/=.*/=…/'

# Google AI Studio path
grep -E '^GOOGLE_AI_STUDIO_(API_KEY|API_URL)=' .env | sed 's/=.*/=…/'

# Confirm S3 mirroring is configured (so successful sidecars push to R2).
grep -E '^(S3_ENDPOINT|S3_BUCKET)=' .env | sed 's/=.*/=…/'

# Smoke reachability + auth — for whichever provider you intend to use.
uv run monitorul-ii analyze pdfs/ --dry-run | head -3
# Expected: "analyze: dry-run (would target openrouter at https://openrouter.ai/api/v1; …)"

# Same smoke against Google AI Studio
uv run monitorul-ii analyze pdfs/ --dry-run --provider google | head -3
# Expected: "analyze: dry-run (would target google at https://generativelanguage.googleapis.com/v1beta; …)"
```

If the chosen provider's API-key env var is missing, the CLI exits with code 2 before any network call (and prints the env var name it expected).

---

## 1. The full launch pattern

```sh
mkdir -p data/analyze-runs
uv run monitorul-ii analyze pdfs/ \
    -j 36 --reverse \
    --budget-usd 50 \
    --log-file data/analyze-runs/run-$(date +%Y%m%d-%H%M).jsonl
```

Breakdown:

| Flag | Why |
|---|---|
| `pdfs/` | Walks every `*.extraction.json` in the directory (recursively flat). |
| `-j 36` | 36 worker threads. Sweet spot for OpenRouter Flash-Lite (cap 200-450 c/m); -j 48 hits the rate limit. |
| `--reverse` | Walks newest→oldest. Backfills recent corpus first; combined with `--budget-usd` this gives "the most recent N months your budget covers". |
| `--budget-usd 50` | Soft cap on cumulative spend. Producer exits cleanly when the cap is hit; resume by re-running. |
| `--log-file PATH` | Per-speech JSONL telemetry. `tail -f` to watch live activity. |

Other useful flags:

| Flag | Use |
|---|---|
| `--force` | Re-code every record, ignoring fingerprint matches. Use after a prompt-version bump. |
| `--dry-run` | Walk + count without contacting OpenRouter. Safe in CI. |
| `--limit N` | Process at most N sidecars (after `--reverse` is applied). Spike runs. |
| `--max-words N` | Skip speeches above N words (default 800; v0.2 will chunk). |
| `--retry-on-error N` | Retry budget per LLM call (default 1). For transport / json_parse / schema_invalid; **NOT** for 429s — those have their own budget. |
| `--provider {openrouter,google}` | Backend selector. Default `openrouter`; pass `google` to route through Google AI Studio's native API (~25% cheaper at Flash-Lite rates; needs `GOOGLE_AI_STUDIO_API_KEY`). Both produce identical discourse JSON. |
| `--openrouter-url URL` | Endpoint override (used when `--provider openrouter`). |
| `--google-url URL` | Endpoint override (used when `--provider google`). |
| `--model NAME` | Override the model. Defaults: `google/gemini-3.1-flash-lite` for OpenRouter, `gemini-3.1-flash-lite` for Google AI Studio. |
| `--no-upload` | Skip S3 mirror even when env vars are set. |

---

## 2. Live observation

Two terminals: one for the run, one for the tail.

### A. The progress bar (run terminal)

The `_AnalyzeProgressReporter` rich bar shows:

```
71%   3,945/5,556 · ana=12,840 reuse=687 fail=42 long=15 skip=1,196 err=0
                  · 380rec/m 1240c/m @2800ms · 18.2Min 920Kout
                  · $42.15 (3.21$/m) · H+=2,304 H-=10,536 (vskip=82%)
                  · 429×127 fb×12,840  · s3 up=3,231 have=11,609 err=0
```

| Signal | What it tells you |
|---|---|
| `ana=N` | New records freshly coded this run |
| `reuse=N` | Fingerprint-matched records (no API cost) |
| `fail=N` | Records dropped this run after retries exhausted (will be retried on next run) |
| `long=N` | Records skipped above `--max-words` |
| `skip=N` | Sidecars skipped entirely (zero eligible speeches OR all already coded) |
| `Nrec/m` | Speech throughput. ETA = `(remaining records) / (rec/m)`. |
| `Nc/m` | LLM call rate. Watch this against OpenRouter's 200-450/min ceiling. |
| `@Nms` | Avg per-call latency. >5s = upstream queueing. |
| `Min Kout` | Cumulative tokens. Sanity-check against OpenRouter's invoice. |
| `$N (M$/m)` | Cost from OpenRouter's authoritative `usage.cost`. M$/m projects budget timing. |
| `H+=N H-=N (vskip=N%)` | Hawkins-marker distribution. >70% no-marker = corpus baseline (expected). |
| `429×N fb×N` | Rate-limit retries × json_object fallbacks. fb≈c/m means every call needs Gemini schema-recovery (expected). |

### B. The JSONL tail (observer terminal)

Each coded speech writes one JSON line to the log file. Most useful tail patterns:

```sh
# Newest run's log file
LOG=$(ls -t data/analyze-runs/run-*.jsonl | head -1)

# 1. Compact view of every speech as it lands
tail -qf "$LOG" | jq -cR 'fromjson? | {
    ts, sidecar, record_id,
    h: .hawkins_score, v: .vparty_score, dqi: .dqi_level,
    cost: .cost_usd
}'

# 2. Just high-Hawkins speeches (where the editorial signal lives)
tail -qf "$LOG" | jq -cR 'fromjson? | select(.hawkins_score >= 1)'

# 3. Per-sidecar progress (refresh every 5s)
watch -n 5 "jq -r .sidecar '$LOG' | sort | uniq -c | sort -rn | head -20"

# 4. Live cost burn-rate
tail -qf "$LOG" | jq -cR 'fromjson? | .cost_usd' | awk '{sum+=$1; print sum}'

# 5. Failed records (will be re-coded on next run)
tail -qf "$LOG" | jq -cR 'fromjson? | select(.outcome == "failed") | .record_id'

# 6. Hawkins/V-Party distribution snapshot
jq -sR 'split("\n") | map(select(length > 0) | fromjson)
        | group_by(.hawkins_score) | map({h: .[0].hawkins_score, n: length})' "$LOG"
```

> **Why `tail -qf` + `jq -R 'fromjson?'`?** `tail -f` with a glob prints `==> filename <==` headers when matching multiple files; `jq` parses them as input and crashes on the non-JSON. `-q` suppresses headers; `-R` (raw input) + `fromjson?` (try-parse-or-skip) makes the pipeline robust to header lines, partial writes, and log rotation.

### C. The OpenRouter dashboard

```
https://openrouter.ai/activity
```

Real-time per-call latency, tokens, model, cost. Authoritative source for billing.

### D. The disk-flush watch

The producer flushes the discourse file every 5 successfully-coded records (per `PARTIAL_FLUSH_EVERY`). Watch which sidecars are growing:

```sh
watch -n 5 'find pdfs/ -name "*.discourse.flash-lite.v0_1.json" \
    -mmin -2 -printf "%TH:%TM  %f\n" | sort | tail -10'
```

---

## 3. Stopping & resuming

### Ctrl+C is safe (with the v0.1.x fix)

The producer flushes its discourse file every 5 successfully-coded records. On `KeyboardInterrupt` (or any `BaseException`), the in-memory buffer flushes once more before the signal propagates. Worst-case data loss on a hard kill at -j 36:

```
PARTIAL_FLUSH_EVERY × workers ≤ 5 × 36 = 180 records
(vs ~1,800 records before the periodic-flush fix)
```

### Re-running after a stop

```sh
# Just relaunch with the same command. Records persisted to disk are
# fingerprint-matched and skipped. Records lost to Ctrl+C are absent
# from disk → no fingerprint match → re-coded.
uv run monitorul-ii analyze pdfs/ -j 36 --reverse --budget-usd 50 \
    --log-file data/analyze-runs/run-$(date +%Y%m%d-%H%M).jsonl
```

The contract: **re-running NEVER recodes work that was successfully persisted**. It only redoes:
- Records lost to Ctrl+C / hard kill
- Records that hit errors and were dropped (per the "save what succeeded, retry the rest" rule)
- Records whose text changed (re-extract changed the sidecar body)

### Auditing what you'd lose vs save before stopping

```sh
# Cost-already-paid for in-memory work that hasn't flushed yet
LOG=$(ls -t data/analyze-runs/run-*.jsonl | head -1)
.venv/bin/python <<EOF
import json
from pathlib import Path
from collections import defaultdict

coded = defaultdict(set)
total_cost = 0.0
for line in open("$LOG"):
    e = json.loads(line)
    coded[e["sidecar"]].add(e["record_id"])
    total_cost += e["cost_usd"]

persisted = {}
for f in Path("pdfs").glob("*.discourse.flash-lite.v0_1.json"):
    sc = f.name.replace(".discourse.flash-lite.v0_1.json", ".extraction.json")
    persisted[sc] = set(json.loads(f.read_text()).keys())

lost = sum(len(coded[sc] - persisted.get(sc, set())) for sc in coded)
print(f"records coded: {sum(len(v) for v in coded.values())}")
print(f"records persisted: {sum(len(v) for v in persisted.values())}")
print(f"records that would be lost on Ctrl+C: {lost}")
print(f"~\$ wasted (would re-code): {total_cost * lost / max(1, sum(len(v) for v in coded.values())):.4f}")
EOF
```

---

## 4. Failure modes and recovery

### 4.1 Rate-limit storm (429 errors climbing)

**Signal**: `429×N` rises faster than `c/m` in the progress bar.

**Cause**: OpenRouter throttling. Free-tier Flash-Lite caps at ~200 calls/min during peak demand (drops from the nominal 450).

**Behaviour**: The producer absorbs 429s with a dedicated retry loop (15→30→60→90→120s backoff, honouring `Retry-After` header). Records aren't failed unless the 6-attempt rate-limit budget is exhausted. The user-visible `--retry-on-error` budget is for transport / parse failures, NOT 429s.

**Action**: If 429 retries are dragging throughput below useful levels, lower `-j` to give OpenRouter breathing room.

### 4.2 Schema fallback on every call (`fb×N == c/m`)

**Signal**: `fb×N` equals or nearly equals `c/m`.

**Cause**: Gemini's strict-mode rejects our JSON Schema. The producer falls back to `json_object` automatically. No data loss — every call still completes successfully.

**Behaviour**: This is **expected** for `gemini-3.1-flash-lite`. The first call (strict) is FREE (Gemini doesn't bill 400s before inference); the json_object retry is billed. Net: same cost as if strict worked, +1 round-trip per call.

**Action**: None. The cost in real `usage.cost` reflects the actual billed calls.

### 4.3 Per-sidecar `fail=N` accumulating

**Signal**: `fail=N` rises in progress bar; entries appear with `outcome=failed` in the JSONL.

**Cause**: Both the per-call retry budget AND the rate-limit retry budget exhausted (transient network failure, upstream provider 5xx).

**Behaviour**: Failed records are **NOT persisted** to the discourse file. They're dropped from this run. The next run will see no fingerprint match → re-codes them.

**Action**: Re-run after the run completes. Fingerprint-match short-circuits the successful 99%; only the failed 1% get retried.

### 4.4 Budget exhausted

**Signal**: `$N` >= `--budget-usd` cap; progress bar shows `$$$ {sidecar} budget exhausted`.

**Behaviour**: Producer finishes the current sidecar atomically, exits with code 0, emits the final summary line + a "resume by re-running" hint.

**Action**: Re-run with a higher cap (or wait for the next budget cycle). Fingerprint-match preserves all prior work.

### 4.5 OpenRouter unreachable at startup

**Signal**: CLI exits with code 2 before processing any sidecar.

**Cause**: Network issue, expired API key, or `OPENROUTER_URL` misconfigured.

**Action**: The healthcheck (`GET /models`) is run before the work loop. Fix the connection and re-run.

---

## 5. Tuning concurrency (`-j N`)

OpenRouter's per-key rate limit is the binding constraint, not your local CPU.

| `-j N` | Throughput pattern | When |
|---|---|---|
| `1` | Sequential. Fully predictable, no rate-limit risk. | Debugging; per-sidecar latency measurement. |
| `4-8` | ~30-100 c/m. Conservative. | Small-budget runs; shared OpenRouter key. |
| `12-24` | ~100-200 c/m. Steady. | Most production backfills. |
| `36-48` | ~200-400 c/m, bumping the rate limit. | Aggressive backfill; the 429 handler absorbs spikes. |
| `>48` | 429s dominate; throughput plateaus. | Only if you have a paid OpenRouter tier with higher per-key concurrency. |

The progress bar tells you when to back off:

| If you see | Then |
|---|---|
| `c/m` rising as you bump `-j` | Producer-side underutilised; keep pushing. |
| `c/m` plateauing at 200-450 | Hit OpenRouter's ceiling. Stop pushing. |
| `429×N` rising fast | Rate-limit backoff active. Lower `-j` by 50%. |
| `@ms` climbing >5s | Upstream queueing. Lower `-j` slightly. |

**Empirical at v0.1**: -j 36 hits 200-380 c/m on Flash-Lite during typical-load hours, with 429 rates around 5-15 per minute (absorbed silently). Throughput drops to ~150 c/m during peak demand windows.

---

## 6. Skip line vocabulary

Every sidecar prints one line in the progress log. The leading tag tells you what happened:

```
ok    <sidecar>  [ana=N reuse=N fail=N long=N $X.XXXX Ncalls@Nms]   ← new work persisted
reuse <sidecar>  [reuse=N]                                          ← every record was fingerprint-matched (already coded)
skip0 <sidecar>  [no eligible speeches]                             ← committee/qr/report — no speeches to code; no discourse file ever created
fail  <sidecar>  [all N records failed; keeping prior M clean]      ← every record this run hit an error
ERR   <sidecar>  (msg)                                              ← sidecar-level failure (read error, missing document_id)
$$$   <sidecar>  budget exhausted ($X.XXX total)                    ← `--budget-usd` cap hit mid-sidecar
dry   <sidecar>  [would-code=N reuse=N]                             ← `--dry-run` mode
```

**~21% of the 5,556-sidecar corpus is `skip0`** (committee summaries / question registers / report facsimiles). They print instantly at run launch and don't move the cost needle.

---

## 7. After the run completes

The discourse JSON files are now alongside the sidecars. To project them into Elasticsearch:

```sh
# IF the mo-speeches mapping has changed since the last bootstrap (e.g.
# new discourse marker fields landed via a code change), push the diff
# to the live cluster first. Additive-only, idempotent — safe to run
# without checking. Skip if you're sure the mapping is current.
uv run monitorul-ii es-init --update-mappings

# Re-index — the indexer's idempotency triple includes
# enrichment_fingerprint, which changed when discourse files appeared.
# Only sidecars with new discourse data are touched.
uv run monitorul-ii index pdfs/ -j 16

# After a mapping bump, force-reindex the affected docs so the new
# fields populate (ES does NOT retroactively re-analyze on put_mapping).
# Only needed when the mapping changed; skip on a steady-state catch-up.
# Timing: ~7 min for the full 5552-doc corpus on -j 16 (the v0.2.0
# position_in_document precedent — same shape of work).
uv run monitorul-ii index pdfs/ --force -j 16
```

Verify in ES:

```sh
# Sample a discourse-coded speech: aggregates + per-marker arrays
uv run monitorul-ii query --name search_speeches --params '{"page_size": 1}' \
    | jq '.hits[0]._source.enrichments.discourse'
# Expect: hawkins/vparty/dqi each carry {score, framework_confidence,
# framework_version, rationale, marker_count, marker_kinds[], markers[]};
# voice carries {dominant_voice, voices_seen[], classifications[]};
# each marker has evidence.{text, char_range} for inline highlighting.

# Cross-tab Hawkins × V-Party
.venv/bin/python <<EOF
from monitorul_ii.elasticsearch.config import ESConfig
from monitorul_ii.elasticsearch.client import build_client
from dotenv import load_dotenv; load_dotenv('.env')
es = build_client(ESConfig.from_env())
res = es.search(index='mo-speeches', body={
    'size': 0,
    'query': {'exists': {'field': 'enrichments.discourse'}},
    'aggs': {
        'hawkins': {
            'terms': {'field': 'enrichments.discourse.hawkins.score', 'size': 5},
            'aggs': {'vparty': {'terms': {'field': 'enrichments.discourse.vparty.score', 'size': 5}}}
        }
    }
})
for h in res['aggregations']['hawkins']['buckets']:
    for v in h['vparty']['buckets']:
        print(f'H={h["key"]} V={v["key"]}: {v["doc_count"]:,}')
EOF

# Sanity-check char_range slicing — pick a discourse-coded speech and
# confirm the evidence text matches the slice into speech.text. Catches
# off-by-one or matcher regressions before the web app surfaces them.
.venv/bin/python <<EOF
from monitorul_ii.elasticsearch.config import ESConfig
from monitorul_ii.elasticsearch.client import build_client
from dotenv import load_dotenv; load_dotenv('.env')
es = build_client(ESConfig.from_env())
hit = es.search(index='mo-speeches', body={
    'size': 1,
    'query': {'exists': {'field': 'enrichments.discourse.hawkins.markers'}}
})['hits']['hits'][0]
src = hit['_source']
text = src['text']
for m in src['enrichments']['discourse']['hawkins'].get('markers', []):
    ev = m.get('evidence', {})
    cr = ev.get('char_range')
    if cr:
        slice_ = text[cr[0]:cr[1]]
        match = '✓' if slice_ == ev['text'] else '✗'
        print(f"{match} {m['kind']}: {ev['text'][:40]!r} == {slice_[:40]!r}")
    else:
        print(f"… {m['kind']}: char_range omitted (paraphrase)")
EOF
```

See `docs/discourse-and-semantic-search.md` for the canonical query patterns and the per-speech rendering pseudocode.

---

## 8. Cleanup of orphaned processes

If you killed the analyze run with `kill -9` or the terminal crashed, check for orphaned workers:

```sh
# 1. Active analyze processes
ps -ef | grep "monitorul-ii analyze" | grep -v grep

# 2. Orphaned multiprocessing workers (parent=1 = init = orphaned)
ps -ef | grep -E "spawn_main|resource_tracker" | grep -v grep

# 3. Open .part files (atomic-rename leftovers if a hard kill happened
# mid-write; should be empty)
find pdfs/ -maxdepth 1 -name '*.part'

# 4. Open OpenRouter connections (should be 0 after stop)
ss -tn | grep -i openrouter

# 5. Kill orphans (TERM first, KILL if needed)
kill <PID1> <PID2> ...
sleep 2
kill -9 <PID1> <PID2> ...   # only if they survived TERM

# 6. Remove any orphan .part files
find pdfs/ -maxdepth 1 -name '*.part' -delete
```

The producer's atomic-rename contract guarantees no half-written discourse files even on hard kill — `.part` files are removed by the rename system call before the producer ever returns. So leftover `.part` files indicate the kill happened DURING the rename (rare).

---

## 9. Quick reference

```sh
# Standard production launch
uv run monitorul-ii analyze pdfs/ -j 36 --reverse --budget-usd 50 \
    --log-file data/analyze-runs/run-$(date +%Y%m%d-%H%M).jsonl

# Live tail
tail -qf "$(ls -t data/analyze-runs/run-*.jsonl | head -1)" \
    | jq -cR 'fromjson? | {ts, sidecar, record_id,
                           h:.hawkins_score, v:.vparty_score,
                           dqi:.dqi_level, cost:.cost_usd}'

# Resume after Ctrl+C / budget cap (same command — fingerprint-match handles the rest)
uv run monitorul-ii analyze pdfs/ -j 36 --reverse --budget-usd 50 \
    --log-file data/analyze-runs/run-$(date +%Y%m%d-%H%M).jsonl

# Project new discourse data into ES
uv run monitorul-ii index pdfs/ -j 16

# Force re-code (after a prompt-version bump)
uv run monitorul-ii analyze pdfs/ --force --reverse --budget-usd 50 \
    --log-file data/analyze-runs/run-$(date +%Y%m%d-%H%M).jsonl

# Spike test on 5 newest sidecars
uv run monitorul-ii analyze pdfs/ --reverse --limit 5 --no-upload

# Dry-run to estimate cost / scope
uv run monitorul-ii analyze pdfs/ --dry-run --reverse --limit 100
```

---

## Appendix A — JSONL log entry shape

Every coded speech writes one line:

```json
{
  "ts": "2026-05-09T10:34:21+00:00",
  "sidecar": "2026-04-24_MO-PII-45-2026.extraction.json",
  "record_id": "mo://2026/II/45#agenda-3#act-12",
  "outcome": "ok",
  "hawkins_score": 0,
  "hawkins_markers": 0,
  "vparty_score": 0,
  "vparty_markers": 0,
  "dqi_level": 1,
  "voice_ran": false,
  "tokens_in": 18234,
  "tokens_out": 1547,
  "cost_usd": 0.004215,
  "calls": 3,
  "fallbacks": 3,
  "rate_limit_retries": 0,
  "repaired": 0,
  "errors": []
}
```

Field semantics:

| Field | Meaning |
|---|---|
| `ts` | ISO 8601 UTC timestamp when the speech finished coding |
| `sidecar` | Source sidecar's basename (find it via `find pdfs/ -name "$sidecar"`) |
| `record_id` | Canonical ID per `src/monitorul_ii/extraction/identity.py` |
| `outcome` | `ok` (persisted to disk) or `failed` (dropped, will be retried next run) |
| `hawkins_score` | 0/1/2 holistic populism grade. `null` if Hawkins call failed. |
| `hawkins_markers` | Count of Hawkins markers emitted (0 = voice pass skipped) |
| `vparty_score` | 0/1/2 holistic anti-pluralism grade |
| `dqi_level` | 0-3 level of justification |
| `voice_ran` | `true` iff Hawkins emitted ≥1 marker AND voice classifier ran |
| `tokens_in` / `tokens_out` | Cumulative across this speech's 3-4 prompts |
| `cost_usd` | OpenRouter authoritative cost (from `usage.cost`) |
| `calls` | LLM round-trips for this speech (3 if no Hawkins markers, 4 if voice ran) |
| `fallbacks` | json_object fallback firings (≈ calls for Gemini) |
| `rate_limit_retries` | 429s absorbed by the rate-limit retry loop |
| `repaired` | json-repair fallback firings (rare; non-zero hints at upstream model degradation) |
| `errors` | Per-prompt error strings if any failed (only when `outcome=failed`) |

The file is **append-only** — re-runs add more lines without touching prior entries. Use `tail -qf` to monitor live; use `jq -s` for post-run aggregation.

---

## Appendix B — Producer constants reference

Live in `src/monitorul_ii/extraction/enrichments/discourse.py`:

```python
DISCOURSE_NAMESPACE = "discourse"      # filename + ES enrichments key
DISCOURSE_PRODUCER  = "flash-lite"     # filename + _meta.producer
DISCOURSE_VERSION   = "0_1"            # filename version
DISCOURSE_MODEL     = "google/gemini-3.1-flash-lite"

PROMPT_VERSIONS = {
    "hawkins": "v1",
    "voice":   "v1",
    "dqi":     "v1",
    "vparty":  "v2",   # v1 stays in tree for reproducibility; v2 is production
}

OPENROUTER_FLASH_LITE_INPUT_RATE_USD  = 0.10 / 1_000_000
OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD = 0.40 / 1_000_000

MIN_TEXT_CHARS         = 100      # Substantive cutoff
DEFAULT_MAX_WORDS      = 800      # Long-tail filter
DEFAULT_MAX_TOKENS     = 16384    # max_tokens per LLM call
PARTIAL_FLUSH_EVERY    = 5        # Records between flush points (Ctrl+C safety)
RATE_LIMIT_MAX_RETRIES = 6        # 429-retry budget (separate from --retry-on-error)
RATE_LIMIT_BACKOFF_SCHEDULE = (15, 30, 60, 90, 120, 120)  # seconds
```

Tuning these requires a code change + restart. The most operationally relevant:

- **`PARTIAL_FLUSH_EVERY = 5`**: bounds Ctrl+C data loss. Lower = safer but more disk writes; higher = fewer writes but more lost on kill.
- **`RATE_LIMIT_BACKOFF_SCHEDULE`**: matches OpenRouter's per-minute window. Don't go below 15s — you'll fight the rate limit instead of waiting it out.
- **OpenRouter rates**: tune after observing your actual invoices vs the producer's `cost_estimate_usd`. The default is conservative.
