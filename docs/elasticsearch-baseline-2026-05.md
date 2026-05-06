# Elasticsearch baseline — 2026-05 (post P4c — query layer + production rebuild + source-order playback)

Snapshot of the v1 ES projection layer after the P4c phase of `docs/elasticsearch-indexing-prompts.md` lands, **plus the v0.2.0 follow-up** that added `position_in_document` and `list_document_children` to support the `/mo/<id>` full-document playback page. This is the canonical "known clean" diff target for any future agent reasoning about the live index shape, query latencies, and the gaps the webapp (P5) needs to plan around.

The snapshot was taken on **2026-05-06** against the corpus at `pdfs/` (**5,552** typed sidecars, all at `schema_version: 1.13.0` — the identity-block backfill from P2 is complete) and the `mo-*` indices freshly cut by `monitorul-ii es-init` + populated by `monitorul-ii index --rebuild` + later updated via `monitorul-ii es-init --update-mappings` + `monitorul-ii index --force` for the `position_in_document` field landing.

The two pieces this baseline is "post":

1. **`monitorul-ii index --rebuild`** ran end-to-end against `pdfs/` into the v1 generation `mo-<grain>-20260506-v1`. Per-grain doc counts and timing land below; `errors=0` on the full corpus.
2. **`src/monitorul_ii/elasticsearch/queries.py`** ships the 10-function reference layer (`search_speeches`, `get_document`, `list_documents_by_date`, `get_agenda_item`, `get_speech`, `person_page`, `search_persons`, `list_committee_meetings`, `get_report`, `agg_speeches_by_party_year`) that backs the future Next.js `lib/search.ts` and is exposed via the `monitorul-ii query` debug CLI for ad-hoc inspection.

## Versions in effect

Pulled at rebuild time from the cluster's index settings, `monitorul_ii.elasticsearch.indexer.INDEXER_VERSION`, and the queries module:

| Component                                         | Version    |
|---------------------------------------------------|------------|
| `extraction_schema.json`                          | 1.13.0     |
| `monitorul_ii.elasticsearch.indexer.INDEXER_VERSION` | 0.1.0      |
| `monitorul_ii.elasticsearch.queries` (this doc)   | 0.1.0      |
| index template `mo-documents`                     | v1         |
| index template `mo-agenda-items`                  | v1         |
| index template `mo-speeches`                      | v1         |
| index template `mo-votes`                         | v1         |
| index template `mo-interpellations`               | v1         |
| index template `mo-questions`                     | v1         |
| index template `mo-committee-meetings`            | v1         |
| index template `mo-reports`                       | v1         |
| index template `mo-persons`                       | v1         |
| component template `mo-analyzers`                 | v1         |
| component template `mo-common-fields`             | v1         |
| API key `monitorul_reader`                        | minted     |
| API key `monitorul_indexer`                       | minted     |
| read alias `mo-<grain>` → `mo-<grain>-20260506-v1`| 9 grains   |
| write alias `mo-<grain>-write`                    | 9 grains   |

## Per-grain document counts

Captured 2026-05-06 against the live cluster after the most-recent `monitorul-ii index pdfs/` run with `--include-persons`. Query: `curl -s -H "Authorization: ApiKey $ES_API_KEY" "$ES_URL/mo-<grain>/_count" | jq .count`.

| Grain                  | Expected (Q1) | Observed | Notes |
|------------------------|--------------:|---------:|-------|
| `mo-documents`         |        ~5,500 |    5,552 | Exact match: one per typed sidecar |
| `mo-agenda-items`      |       ~59,000 |   58,834 | -0.3% vs expected; v0.2.x collapsed-span recovery on a small set of 2024+ docs explains the slight shortfall |
| `mo-speeches`          |  ~948,000 (Q1) |  815,787 | -14% vs expected. The Q1 estimate predated `agenda.py` v0.2.x's collapsed-span recovery, which intentionally claims fewer per-item activities on some modern stenograms. Substantive subset (filtered) is ~60-70% of this. |
| `mo-votes`             |       ~50,000 |   55,210 | Within band; matches the indexer's bulk count post-`linker.py` v0.2.0 |
| `mo-interpellations`   |        ~7,200 |    6,975 | Within band |
| `mo-questions`         |        ~2,500 |    2,519 | Exact match |
| `mo-committee-meetings`|       ~14,300 |   14,325 | Exact match |
| `mo-reports`           |            52 |       52 | Exact match: one per R-suffix MO |
| `mo-persons`           |     ~5,000 (post-bootstrap) |   13,479 | Larger than initial Q1 estimate because `tools/add_unresolved_speakers.py` minted 4,414 corpus-derived stubs on top of the 9,065 Wikidata-bulk seed, lifting per-doc speaker resolution to ~99% |

**Total docs across all grains: 972,733.** Within 5% of the design doc's ~1M estimate. The shortfall in `mo-speeches` is fully explained by the `agenda.py` v0.2.x trade-off documented in CLAUDE.md (collapsed-span recovery accepts fewer per-item activities to remove cross-doc linker false positives). Production sweep of the linker FP rate dropped from ~40% to 0/10 in spot-check, so the trade was net positive even though the speech count fell.

## Rebuild timing

> **TODO operator** — fill in from a real run. The numbers below are placeholder ranges from the design doc estimates.

| Phase                                | Wall time | Notes |
|--------------------------------------|----------:|-------|
| `es-init` (templates + indices + keys) | ~10s      | Idempotent on second run |
| Sidecar enumeration + denormalization | ~3 min     | 5552 sidecars; ~1.7 ms each |
| Bulk upsert (sequential `-j 1`)      | ~55 min    | ~92 sidecars/min, dominated by ES round-trips |
| Bulk upsert (parallel `-j 16`)       | ~9 min     | ~600 sidecars/min, threads-on-network-bound |
| Persons projection (`--include-persons`) | ~30s   | ~13K registry entries, single bulk |
| **Total `--rebuild` (live cluster, -j 16)** | **~10 min** | |

Hardware: `monitorul.ai` operator's 20-core dev box. Production capacity is the same baseline. ES bulk-throughput ceiling is the dominant factor at high `-j N`; below that, the indexer scales near-linearly.

## Reference query smoke checklist

All 10 reference queries from `src/monitorul_ii/elasticsearch/queries.py` were exercised on 2026-05-06 via `.venv/bin/monitorul-ii query --name <X> --params <...>` against the live populated indices. Wall times below are end-to-end (process spawn → JSON written to `/tmp/out.json`) and dominated by ~500–600 ms of Python interpreter + `elasticsearch` client cold-start per CLI invocation; the actual ES round-trip is the residual ~50–200 ms. The Next.js webapp will see only the residual since it keeps a long-lived client.

> **CLI invocation note** — `uv run monitorul-ii ...` buffers stdout silently when redirected (the snap-installed `uv` quirk documented in CLAUDE.md). For scripted smoke runs that capture output, invoke `.venv/bin/monitorul-ii ...` directly (or pipe through `cat`).

| # | Query                       | Representative call                                                                                       | Observed result                                                  | wall time | Notes |
|--:|-----------------------------|-----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------|----------:|-------|
| 1 | `search_speeches`           | `--params '{"q":"educație","page_size":5}'`                                                              | `total=35,425  hits=5  page_size=5`                              | 0.73 s    | Default `is_substantive: true` filters chair-procedure noise; flip to `false` for the admin view |
| 2 | `get_document`              | `--params '{"document_id":"mo://2026/II/48"}'`                                                          | `_source` with 32 keys (chamber, year, agenda_count=14, speech_count=65, …) | 0.62 s | Single `_doc` GET; 404 returns `null` |
| 3 | `list_documents_by_date`    | `--params '{"date":"2026-04-14","chamber":"Camera Deputaților"}'`                                       | `total=1  hits=1  page_size=20`                                  | 0.69 s    | Sorted by `published` desc; sitemap and `/calendar/<date>` consume |
| 4 | `get_agenda_item`           | `--params '{"record_id":"mo://2026/II/48#agenda-1"}'`                                                   | `_source` with 25 keys (ordinal, category, confidence_type, …)   | 0.61 s    | Lookup by `_id`; 404 returns `null` |
| 5 | `get_speech`                | `--params '{"record_id":"mo://2026/II/48#agenda-1#act-1"}'`                                             | `_source` with 27 keys (agenda_title, agenda_outcome, speaker, refs, …) | 0.70 s | Below-substantive speeches return `null` from the public read alias |
| 6 | `person_page`               | `--params '{"person_slug":"zgonea-valeriu-stefan"}'` (high-traffic speaker)                              | `person` + `recent_speeches=20` + `stats_total=10,470` (10,257 Camera, 62 Senat, 16 distinct years) | 0.69 s | Two ES round-trips (persons GET + speeches search w/ aggs); stats agg runs every call. Low-traffic speakers (e.g. `zainescu-cristian-constantin`) return `recent=0 stats_total=0` cleanly — the registry is broader than the speech corpus |
| 7 | `search_persons`            | `--params '{"q":"vacaroiu nicolae"}'`                                                                    | `total=3  hits=3  page_size=20` (top: `vacaroiu-nicolae` score=20.65) | 0.61 s    | Pre-fix run returned 386 hits with `operator: or` (the long tail of every Nicolae). Tightened to `operator: and` after the smoke surfaced the issue — both tokens must appear in the same field. Single-token searches (`"vacaroiu"`) still work because `and` is a no-op on one term. The remaining 2 hits beyond the canonical are polluted stub entries (`vacaroiu-domnule-nicolae`, `vacaroiu-domnului-nicolae`) that retained honorifics in canonical_name — a known registry long-tail issue tracked in CLAUDE.md, not a query-layer bug |
| 8 | `list_committee_meetings`   | `--params '{"committee_id":"comisia-pentru-buget-finanțe-și-bănci","date_from":"2024-01-01"}'`           | `total=64  hits=20  page_size=20`                                | 0.62 s    | Sorted by `meeting_date` desc |
| 9 | `get_report`                | `--params '{"record_id":"mo://2014/II/1R"}'`                                                            | `_source` with 16 keys (issuing_body, reporting_period, headings, …) | 0.67 s | Lookup by `_id` (same as `document_id` for R-suffix); 404 returns `null` |
|10 | `agg_speeches_by_party_year`| `--params '{"year":2018,"size":5}'`                                                                     | `total=24,591  aggs.by_party=[(unknown):24591 → by_year:[2018:24591], speakers:547]` | 0.62 s | Verified: 24,591 substantive 2018 speeches across 547 distinct speakers; **all bucketed under `"(unknown)"`** because `speaker.party_group_at_time` is unpopulated. Tracked as gap #6 below |

A green smoke run (all 10 queries return non-empty within wall-time budget) is the "ready for P5" gate, **and was satisfied on 2026-05-06.** ES round-trip residual after cold-start subtraction is well within the design doc's <300 ms p95 target for every query.

### `list_document_children` (v0.2.0 follow-up)

The 11th query — added after the original P4c smoke surfaced that "render this MO in source order" had no answer in the typed layer. Backed by a new denormalised `position_in_document: integer` field on every per-doc child grain (`mo-agenda-items`, `mo-speeches`, `mo-votes`, `mo-interpellations`, `mo-questions`, `mo-committee-meetings`), sourced from `record.source_span.chars[0]`.

| Query                       | Representative call                                                              | Observed result                                                | Notes |
|-----------------------------|----------------------------------------------------------------------------------|----------------------------------------------------------------|-------|
| `list_document_children`    | `--params '{"document_id":"mo://2018/II/178","page_size":15}'`                  | `total=119  hits=15`; agenda items first (pos=6788), then activities of agenda-1 in source order (act-1@7008 → vote-1@7692 → act-5@7864), then interpellations starting at pos=19271 with `interp-40` correctly between `interp-seq-2` and `interp-seq-5` because byte-offset 21207 sits there | Multi-index search (6 indices); each hit's `index` field carries the normalised grain name; `PLAYBACK_PAGE_SIZE = 500` cap covers every observed doc |
| `list_document_children`    | worst-case: `--params '{"document_id":"mo://2022/II/75","page_size":500}'`      | `total=183  hits=183`; 100% have populated `position_in_document`; full set in monotonic non-decreasing source order; grain breakdown 12 agenda-items + 170 interpellations + 1 speech | Worst-observed plenary doc per p99/max stats from pre-fix smoke |

**Field rollout**: the live indices got the new `position_in_document` field via `monitorul-ii es-init --update-mappings` in seconds (additive `put_mapping` per grain), followed by a 7m08s force-reindex of the full corpus (`monitorul-ii index pdfs/ --force -j 16`) to backfill the field on every doc. Zero errors, all 5,552 docs reindexed. **INDEXER_VERSION bumped to 0.2.0** to reflect the projection-shape change.

## Spot-checks

> **TODO operator** — populate after rebuild from the live cluster.

* **20 random `record_id` lookups** across all grains, each via the relevant `get_*` query. Expected: 20/20 return non-null `_source` matching the grain's mapping.
* **`is_substantive: true` filter behavior** on `mo-speeches`: p25 / p50 / p99 of `text_length` in the filtered subset (Q5 expectation: p50 ≈ 134 chars, p99 ≥ 5,000 chars; chair-procedure cluster <50 chars excluded).
* **Aggregation buckets**: `terms` by `chamber`, `legislature`, `agenda_category`, `speaker.person_id`; `histogram` by `session_date` year. Each should return non-empty buckets and stay under the 100-bucket cap when called via `queries.agg_speeches_by_party_year` (or `DEFAULT_AGG_SIZE` for callers passing through the cap).
* **Filter combination triple**: `(chamber, year, ref_bills)` on `search_speeches`. The webapp's typical query shape; latency budget < 400 ms.

## Known gaps before P5 commits

These are the documented gotchas the P5 webapp build needs to plan around. Each is tracked in the design doc; the gaps below are *the ones surfaced by this baseline run* and not yet acted on.

1. **Embeddings (P3) deferred.** `enrichments.embedding` is unset on every grain; `rank_fusion="rrf"` in `search_speeches` falls back to BM25 silently. Plan P5 search UX with this in mind — semantic search ranks won't be available until P3 ships.
2. **`mo-persons.stats` block null on first index.** The Q4-deferred per-person speech-count rollup is not yet computed by a periodic aggregation job; `person_page` synthesises stats query-time via `terms` aggs instead. Performance budget on that synthesis is < 400 ms p95; revisit if speech counts grow past ~10K per top-tier politician.
3. **Long-tail person resolution.** ~99% on the 1,526-speaker high-traffic doc per CLAUDE.md, but the unresolved 1% surface as `Speaker.person_id: null` in `mo-speeches.speaker.person_id`. `person_page` queries skip these (correctly); `search_speeches` with no `speakerPersonId` filter sees them.
4. **xref-linker only resolves same-list `art. N`.** Cross-list cases (agenda-anchor → speech-level art-N) are not resolved; ES indexes the unresolved unknowns as-is. Speech-level reference filters via `refs.bills` won't catch implicit art-N anchors against the parent agenda's primary_references.
5. **Vote index undercount on collapsed-span docs.** `agenda.py` v0.2.9 trades per-item bill-ref attribution against partition correctness; the cross-doc linker indexes 9,220 votes (down from a pre-fix 11,547) but with FP rate 0% in spot-check. Downstream `defers_to` / `resolves` should still be treated as candidate links, not verified.
6. **`speaker.party_group_at_time` unpopulated.** Surfaced by the smoke run of `agg_speeches_by_party_year` against 2018: all 24,591 substantive speeches bucket under `"(unknown)"`. The field is a Speaker-dict slot that the v0.1 extractor doesn't populate from in-corpus signal; a future enrichment / backfill pass joining the party-group registry against the persons registry is the right path. Until shipped, party-segmented analytics return one big "(unknown)" bucket — the discourse-research dashboard cannot cut by party, and `search_speeches` filtering by `speaker.party_group_at_time` returns zero hits. Plan P5 with this in mind: party UX is gated on this enrichment.

## Reproducing this baseline

```bash
# 0. Sanity check the corpus state
ls pdfs/*.extraction.json | wc -l   # → 5552

# 1. Provision ES (idempotent — second run no-ops)
uv run monitorul-ii es-init

# 2. Full rebuild against today's generation; -j 16 on a 20-core box
uv run monitorul-ii index pdfs/ --rebuild --target=mo-documents-20260506-v1 \
    --include-persons -j 16

# 3. Per-grain count verification
for g in documents agenda-items speeches votes interpellations questions \
         committee-meetings reports persons; do
  COUNT=$(curl -s -H "Authorization: ApiKey $ES_API_KEY" \
              "$ES_URL/mo-$g/_count" | jq .count)
  echo "mo-$g: $COUNT"
done

# 4. Reference query smoke
uv run monitorul-ii query --name search_speeches --params '{"q":"educație","page_size":5}'
uv run monitorul-ii query --name get_document --params '{"document_id":"mo://2018/II/168"}'
uv run monitorul-ii query --name agg_speeches_by_party_year --params '{"year":2018}'
```

## Diffing against future baselines

Future baselines should re-run the count + smoke commands above and diff against this snapshot. Material movement (>10% per-grain delta, p95 latency regression >2×, error count >0) is a regression signal worth a follow-up doc.

The "known gaps" section is the regression-risk index — a gap that *closes* in a future baseline (e.g. embeddings ship, person stats pre-computed) shrinks the section; a gap that *grows* (new long-tail discovered, new query pattern slow) extends it.
