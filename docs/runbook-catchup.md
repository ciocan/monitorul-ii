# Catch-up runbook

Step-by-step guide to bring the local pipeline + S3 mirror + Elasticsearch index forward when there's a gap. The example walks through closing the **2026-04-15 → 2026-05-08** gap (~3 weeks); substitute your own dates.

The pipeline is **fetch → convert → extract → link → backfill → embed → index**. Every step is idempotent — already-processed days/files are skipped — so running each command serially is safe even if some steps are partially complete.

> **One-shot alternative**: `uv run python tools/catchup.py` runs all 7 stages in sequence with per-stage pre/post checks and writes a JSON report to `data/catchup-reports/catchup-report-<run_id>.json`. Auto-detects the date range from the SQLite audit log (resumes the day after the latest `status='ok'` row). Pass `--include-cleanup` to bundle the Appendix A cleanup. See [§ Quick reference — one-shot catch-up runner](#quick-reference--one-shot-catch-up-runner) at the bottom.

---

## 0. Pre-flight checks

```sh
# Working directory
cd /home/ciocan/projects/monitorul

# Confirm dependencies
uv sync

# Confirm env vars are set (S3 + ES + embed-service URL).
# `.env` is auto-loaded by python-dotenv; .env should have:
#   S3_ENDPOINT, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET
#   ES_URL, ES_API_KEY (the monitorul_indexer key for index ops)
#   EMBED_URL (default http://127.0.0.1:8000 if omitted)
#   PROXY_URL (if you use one for monitoruloficial.ro)
grep -E '^(S3_|ES_|EMBED_URL|PROXY_URL)=' .env | sed 's/=.*/=…/'

# Verify the embed service is up (BGE-M3 FastAPI in services/embed/).
# If it returns a non-200 or hangs, start it before step 6.
curl -sf http://127.0.0.1:8000/healthz && echo OK || echo NOT_REACHABLE

# Verify ES connectivity. (Replace with your actual ES_URL.)
curl -sf -H "Authorization: ApiKey $ES_API_KEY" "$ES_URL/_cluster/health?pretty" | head -10
```

If the embed service isn't running, start it:

```sh
# CPU build (default)
docker compose up -d embed
# wait for it to download the BGE-M3 weights on first start (~2 GB)
docker compose logs -f embed
```

---

## 1. Fetch new PDFs

Walk the date range, downloading every Partea II issue that isn't already on disk + in the SQLite audit log. Empty days (weekends, no-issue days) are recorded so resumes don't re-poll them.

```sh
uv run monitorul-ii fetch 2026-04-15 --until 2026-05-08
```

**What gets created**: `pdfs/<YYYY-MM-DD>_MO-PII-<num>-<year>.pdf` for each issue, mirrored to S3 with `Content-Type: application/pdf` (skip if the bucket already has the key).

**Verification**:
```sh
ls pdfs/2026-04-1[5-9]*.pdf pdfs/2026-04-2*.pdf pdfs/2026-04-3*.pdf pdfs/2026-05-0*.pdf 2>/dev/null | wc -l
sqlite3 data/monitorul.db "SELECT date, status, COUNT(*) FROM days WHERE date >= '2026-04-15' GROUP BY date, status ORDER BY date" | head -30
```

**If a day failed transiently** (5xx / network timeout): re-run the same command. `status='failed'` rows auto-retry. `status='gone'` rows are terminal (the source server returned non-PDF) — use `--retry-gone` if you have reason to believe the source recovered the document.

---

## 2. Convert PDFs to markdown

Parallelised via `ThreadPoolExecutor` (PyMuPDF releases the GIL). Default `-j` is `os.cpu_count()`; on a 20-core box `-j 8` is the throughput plateau.

```sh
uv run monitorul-ii convert pdfs/ -j 8
```

**What gets created**: `pdfs/<basename>.md` next to each PDF, mirrored to S3 with `Content-Type: text/markdown`. Skips when the `.md` already exists and is non-empty (use `--force` to re-convert; usually unnecessary).

**Verification**:
```sh
# Every recent PDF should have a sibling .md
for p in pdfs/2026-04-1[5-9]*.pdf pdfs/2026-04-2*.pdf pdfs/2026-04-3*.pdf pdfs/2026-05-0*.pdf; do
  [ -s "${p%.pdf}.md" ] || echo "MISSING: $p"
done
```

---

## 3. Extract structured JSON sidecars

Sequential by design (regex sweeps hold the GIL). Reads each MD, classifies it into one of the 6 document types, dispatches to the per-type extractor, schema-validates strictly, atomic-writes `<basename>.extraction.json`. Idempotent on extractor versions — already-extracted MDs at the current versions are skipped.

```sh
uv run monitorul-ii extract pdfs/
```

**What gets created**: `pdfs/<basename>.extraction.json` per MD, mirrored to S3 with `Content-Type: application/json`.

**Verification**:
```sh
# Should match the count from step 2
ls pdfs/2026-04-1[5-9]*.extraction.json pdfs/2026-04-2*.extraction.json pdfs/2026-04-3*.extraction.json pdfs/2026-05-0*.extraction.json 2>/dev/null | wc -l

# Spot-check coverage on a recent doc
uv run python -c "
import json, glob
for p in sorted(glob.glob('pdfs/2026-05-0*.extraction.json'))[-3:]:
    sc = json.loads(open(p).read())
    cov = sc.get('coverage', {})
    print(f'{p}  doc_type={sc[\"document_type\"]}  claimed_pct={cov.get(\"claimed_pct\", 0):.3f}')
"
```

If any sidecar fails schema validation it's written to `<basename>.rejected.json` instead — inspect and report; an extractor bug is the usual cause.

---

## 4. Run the linker passes

Three passes in one command: report→session (cross-doc), vote-pair (cross-doc deferral chains), art-N cross-reference (intra-doc). All idempotent.

```sh
uv run monitorul-ii link pdfs/
```

**What gets written** (in place inside each `*.extraction.json`):
- `report_facsimile`: `received_at.received_in_document` (cross-doc match by session date)
- `plenary_*`: vote `defers_to` + `resolves[]` for deferral chains; `unknown.resolved_to.char_offsets` for bare `art. N` references

**Verification**:
```sh
# Pick a recent plenary sidecar; check defers_to populated where expected
uv run python -c "
import json, glob
for p in sorted(glob.glob('pdfs/2026-05-0*.extraction.json')):
    sc = json.loads(open(p).read())
    if sc['document_type'] not in ('plenary_stenogram', 'plenary_joint_session'):
        continue
    n_resolved = sum(
        1 for ai in sc['body'].get('agenda_items', []) for a in ai.get('activities', [])
        if a.get('type') == 'vote' and (a.get('defers_to') or a.get('resolves'))
    )
    print(f'{p}: {n_resolved} vote(s) with cross-doc deferral links')
"
```

---

## 5. Run the backfill passes

Four passes in one command: `issuing_body` (report_facsimile), `ministry` (qr + plenary interpellations), `proposed_by` (Guvern speaker on OUG/OG votes), `persons` (Speaker.person_id from the curated registry). Default `-j` = CPU count.

```sh
uv run monitorul-ii backfill pdfs/ -j 8
```

**What gets written** (in place):
- `report_facsimile.body.report.issuing_body_normalized`
- `qr.questions[].addressee.ministry_normalized`
- `plenary_*.interpellations[].addressed_to_normalized`
- `plenary_*.agenda_items[].activities[].proposed_by` (Guvern Speaker on Government-proposed bills)
- Every `Speaker.person_id` across all six doc types

The persons pass under `--force` ALSO clears stale `person_id` values when the matcher no longer resolves the speaker — the recovery path used for the matcher precision fixes (Vela/Vlad, Florian Nicolae, EDITOR-stub etc.). For a plain catch-up run (just new sidecars), `--force` isn't needed.

---

## 6. Generate BGE-M3 embeddings

Walks every embeddable record (substantive speeches `text_length ≥ 100`, agenda titles, interpellations, qr questions, committee meeting purposes, report headings) and POSTs to the embed service. Idempotent via per-record `text_fingerprint`.

```sh
# Smoke-check the service first
uv run monitorul-ii embed pdfs/2026-05-0*.extraction.json --dry-run

# For real
uv run monitorul-ii embed pdfs/
```

**What gets created**: `pdfs/<basename>.embedding.bge-m3.v0_1.json` per sidecar (mutable — the producer overwrites with `overwrite=True` to S3).

**Throughput**: ~3 hrs per 5552-doc full corpus on a single GPU, ~30 hrs on CPU. The 3-week catch-up should be on the order of 30-50 PDFs → minutes.

If the embed service isn't reachable, the CLI fails fast with exit 2 — start `docker compose up -d embed` and re-run.

---

## 7. Index to Elasticsearch

Walks every sidecar, denormalises across the 9 grains, bulk-upserts via per-grain write aliases. State-tracked via SQLite `es_indexed` table on the triple `(sidecar_content_sha, enrichment_fingerprint, index_generation)` — already-indexed-and-unchanged docs are skipped.

```sh
# Sequential is fine; use -j for parallelism on large catchups
uv run monitorul-ii index pdfs/ -j 16
```

**What goes to ES**: per-doc + per-record documents into `mo-documents`, `mo-agenda-items`, `mo-speeches`, `mo-votes`, `mo-interpellations`, `mo-questions`, `mo-committee-meetings`, `mo-reports`. Embeddings flow into `enrichments.embedding` (1024-dim dense_vector) on the appropriate grains.

**Verification** (count docs in `mo-documents` published since 2026-04-15):
```sh
uv run monitorul-ii query --name list_documents_by_date --params '{"date":"2026-05-08"}'

# Or directly via curl
curl -sf -H "Authorization: ApiKey $ES_API_KEY" \
  "$ES_URL/mo-documents/_count" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"range":{"published":{"gte":"2026-04-15"}}}}'
```

The persons registry projection (`mo-persons`) only changes when `persons.json` itself changes — pass `--include-persons` to re-project it; usually unnecessary on a daily catch-up. **Required this time** because the v0.1.1 cleanup dropped 444 polluted stubs (see appendix).

---

## Appendix A — corpus-wide cleanup for the recent matcher / registry fixes

If you haven't yet run the cleanup pass for the recent fixes (per-token fuzzy + sum cap = 1, plus the 444 polluted stubs dropped in `persons.json` v0.1.1), do so AFTER the catch-up index above. Order matters: the cleanup needs the matcher fix already deployed (it is, in main) and re-runs the persons backfill `--force` across the entire corpus (not just the new days).

```sh
# 1. Re-backfill persons across all sidecars. --force clears stale fills
#    where the new matcher returns no match (Florian Nicolae → niculae-florin
#    etc.) AND overwrites mismatches where the new matcher resolves a
#    different id.
uv run monitorul-ii backfill pdfs/ --kind=persons --force -j 16

# 2. Re-index the whole corpus. --force overrides the idempotency triple
#    so every doc re-projects with the corrected person_id values.
#    --include-persons re-projects mo-persons (drops the 444 stubs).
uv run monitorul-ii index pdfs/ --force --include-persons -j 16
```

Expected impact:
- ~145 Speaker dicts will have their `person_id` cleared (Florian Nicolae class).
- ~3,195 Speaker dicts will have their `person_id` cleared (444-stub cleanup, top offender = `deputatilor-editor-parlamentul-romaniei-camera` with 1,746 refs).
- ~73 Speaker dicts will have `gheorghe-vlad` cleared (Vela/Vlad fuzzy false positive).
- `mo-persons` shrinks from 13,479 → 13,035 documents.

The `/politicieni/` UI should stop showing fake person pages and stop misattributing speeches across these classes.

---

## Appendix B — troubleshooting

**`fetch` skips a day that I know has issues.** Check the SQLite log:
```sh
sqlite3 data/monitorul.db "SELECT * FROM days WHERE date = '2026-04-22'"
```
- `status='ok'`: the date was scraped, so the resume gate skips it. `--force` re-fetches.
- `status='gone'`: the source returned non-PDF. `--retry-gone` resets to `pending`.
- `status='failed'`: transient error. The next normal run retries.

**`extract` reports a sidecar as `.rejected.json`.** Inspect:
```sh
cat pdfs/<basename>.rejected.json | jq '.errors // .'
```
The body shape failed JSON-Schema validation — usually an extractor bug surfaced by an unusual document. Report; do not hand-edit the rejected file.

**`embed` is hanging.** The embed service may be loading model weights on first request (~30 s on CPU). Tail the service logs:
```sh
docker compose logs -f embed
```

**`index` reports zero updates after a backfill change.** The idempotency triple matched — the sidecar's `content_sha` and the enrichment fingerprint hadn't changed. Pass `--force` to override.

**ES `403 / current license is non-compliant for [Reciprocal Rank Fusion (RRF)]`.** The RRF query path needs Platinum+; on basic license, the query layer falls back to client-side RRF automatically (see `queries.py`). If you see this in the indexer, something else is amiss — report.

**Catch-up + cleanup at the same time?** Yes — run the catch-up flow steps 1-7 first (fresh sidecars get clean `person_id` values from the start), then Appendix A to clean the historical corpus. Combining them in one `--force` index sweep also works but the staging makes failure modes easier to diagnose.

---

## Quick reference (one-shot for the catch-up alone)

```sh
cd /home/ciocan/projects/monitorul

uv run monitorul-ii fetch 2026-04-15 --until 2026-05-08
uv run monitorul-ii convert pdfs/ -j 8
uv run monitorul-ii extract pdfs/
uv run monitorul-ii link pdfs/
uv run monitorul-ii backfill pdfs/ -j 8
uv run monitorul-ii embed pdfs/
uv run monitorul-ii index pdfs/ -j 8
```

---

## Quick reference — one-shot catch-up runner

`tools/catchup.py` orchestrates the entire 7-stage flow above with pre/post checks per stage, captures filesystem + DB + ES counts, and writes a structured JSON report.

```sh
cd /home/ciocan/projects/monitorul

# Auto-detect dates (resume from the day after the latest status='ok' row;
# until = today). Default workers = 8; default report path =
# data/catchup-reports/catchup-report-<TIMESTAMP>.json.
uv run python tools/catchup.py

# Explicit dates
uv run python tools/catchup.py --from 2026-04-15 --until 2026-05-08

# Bundle the Appendix A cleanup pass into the same run
uv run python tools/catchup.py --include-cleanup

# Run only specific stages
uv run python tools/catchup.py --only fetch --only convert
uv run python tools/catchup.py --skip embed --skip index   # everything except those

# Don't stop on the first stage failure (default: stop and write a partial report)
uv run python tools/catchup.py --continue-on-error

# Custom report path
uv run python tools/catchup.py --report /tmp/today.json
```

**Pre-checks** that gate each stage (skip with reason on failure):

- `fetch` — `data/monitorul.db` parent directory writable
- `convert` — at least one `pdfs/<date>_*.pdf` exists in the date range
- `extract` / `link` / `backfill` — at least one MD or sidecar in range
- `embed` — embed-service `GET /healthz` returns 200
- `index` — `ES_URL` + `ES_API_KEY` set; `GET ES_URL` returns 200

**Post-checks** captured per stage (visible in the JSON report and in the stderr summary):

| Stage | Key metrics |
|---|---|
| fetch | new PDFs since pre-snapshot, total PDFs, `days_by_status` per range, `latest_ok_date` |
| convert | new MDs, list of unpaired PDFs (PDFs without a sibling `.md`) |
| extract | new sidecars, `rejected_in_range` (must be 0) |
| link | sample-doc forward `defers_to` count, back-link `resolves` count, xref-resolved count |
| backfill | sample-doc `speakers_with_person_id` / `speakers_unresolved` / `resolution_rate` |
| embed | new embedding files, in-range coverage % |
| index | ES doc count in range, latest `published`, latest `document_id` |

**Stderr summary** (final block) shows ✓/✗/· marker, duration, and one headline metric per stage:

```
============================================================
CATCH-UP REPORT
============================================================
run_id:        20260508T131007
date range:    2026-04-15 → 2026-05-08
stages:        7 ok, 0 failed, 0 skipped
total time:    742.5s
es docs:       18 in range; latest published 2026-05-08

per-stage:
  ✓ fetch         3.4s  new_pdfs=18
  ✓ convert      89.1s  new_mds=18
  ✓ extract      52.7s  new_sidecars=18  rejected_in_range=0
  ✓ link         12.0s  vote_defers_to_count=2
  ✓ backfill    132.8s  resolution_rate=0.998
  ✓ embed       321.4s  new_embedding_files=18
  ✓ index       131.1s  es_doc_count_in_range=18
============================================================

wrote: data/catchup-reports/catchup-report-20260508T131007.json
```

**Exit codes**: 0 if every stage is `ok`, 1 if any stage failed (report still written), 2 if argparse / pre-flight rejected the inputs.
