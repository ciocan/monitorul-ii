# Elasticsearch indexing — projecting canonical sidecars into a search-and-SEO surface

Companion to [`architecture.md`](./architecture.md), [`extraction-schema.md`](./extraction-schema.md), and [`discourse-analysis-schema.md`](./discourse-analysis-schema.md). The architecture doc records the fetch + convert pipelines. The extraction-schema doc records markdown → canonical JSON. The discourse-analysis doc records canonical JSON → analysis sidecars. **This file documents the next layer: canonical JSON + enrichment sidecars → Elasticsearch indices** that power both `monitorul.ai` (public website, SEO surface) and the in-app LLM agent.

This doc is the design record. Every shape decision is paired with the rationale that produced it, the alternatives considered, and the failure mode each alternative would have produced. The consolidated mappings appear at the end. Future contributors reading this should be able to reconstruct *why* the layer is shaped the way it is — not only *what* it is.

## Mission

ES is **not** the system of record. The sidecar JSON files (canonical extraction + enrichment producers) on disk + S3 are SOT. ES is a **derived projection**: a denormalized, search-optimized, SEO-publishable surface that can be rebuilt from sidecars overnight if it ever evaporates.

The layer must enable, simultaneously, three consumers:

1. **Public website (`monitorul.ai`)** — server-rendered Next.js, static-where-possible (ISR), Romanian-first content, well-indexed by Google. Page surfaces: per-MO document, per-agenda-item debate, per-speech, per-politician, per-committee-meeting, per-interpellation, per-question, per-report.
2. **LLM agent layer** — in-app conversational tools (`monitorul_search_speeches`, `monitorul_get_document`, etc.) that hit the same data with structured queries; rate-limited per conversation; audited.
3. **Researcher / journalist queries** — composite questions like "rank Romanian deputies by Hawkins-graded populism in 2020" (per discourse-analysis-schema.md), "find every speech mentioning Codul muncii art. 65", "track Party X's rhetoric across legislatures". Aggregation-heavy, often filterable across many dimensions.

The layer must absorb three streams of upstream change without breaking SEO, agent contracts, or researcher reproducibility:

- **Extraction fixes** — regex tweaks, new boilerplate patterns, schema bumps. Re-extraction rewrites sidecar bodies; ES must reconcile without orphaning records or breaking URLs.
- **LLM enrichment producers** — topic classifier, summaries, NER, embeddings, discourse-analysis codings. Each producer is independently versioned, iterates faster than the canonical schema, may have parallel competing versions during prompt tuning.
- **Agent runtime writes** — cached chat answers, user-triggered analyses persisted as enrichments. Append-only journal, never raced with batch jobs.

Mission constraints inherited from the upstream schemas apply unchanged:

- **Source-of-truth purity**: ES carries projections, never canonical data. Lose ES → rebuild from disk.
- **Per-occurrence truth**: speaker.party_group_at_time is what was attributed in the source MO, not "current party". This applies down to ES doc fields.
- **Defamation safety**: speech-act codings, not personality attributions. The `mo-persons` index never carries pre-aggregated populism scores; aggregations are query-time, framed as "discourse-density measures of N coded speech-acts".

Two new constraints are specific to this layer:

- **URL stability**: every URL ever published by `monitorul.ai` must keep resolving across re-extractions, schema bumps, and content rewrites. SEO depends on stable canonical URLs over months and years.
- **Reindex-without-downtime**: the layer must support major mapping changes (new dense_vector dim, new analyzer config) and schema migrations without taking the site offline or returning stale results.

## Design tree

The layer was designed by walking nine decisions top-down. Each subsection records the decision, rejected alternatives, and the failure mode each rejection would have produced. The keystone safety decision is **Q2 (stable record IDs persisted in sidecar)** because URL drift across re-extractions is the failure mode that decimates SEO and agent contracts simultaneously.

### Q1. Indexing granularity — multi-grain, seven indices

**Decision.** Seven Elasticsearch indices, each keyed on a natural record type that exists in the canonical extraction and maps 1:1 to a URL shape on `monitorul.ai`:

| Index | Approx docs | URL shape | Source record |
|---|---|---|---|
| `mo-documents` | ~5,500 | `/mo/<year>/<part>/<issue>` | the sidecar envelope (one per MO) |
| `mo-agenda-items` | ~59,000 | `/mo/<year>/<part>/<issue>/agenda/<ord>` | `agenda_items[]` from plenary bodies |
| `mo-speeches` | ~948,000 (only `is_substantive: true` is publicly indexed) | `/discurs/<slug>-<short_id>` | `agenda_items[].activities[]` with `speech` shape |
| `mo-votes` | ~50,000 | `/vot/<short_id>` | `agenda_items[].activities[]` with vote shape |
| `mo-interpellations` | ~7,200 | `/interpelare/<slug>-<short_id>` | `body.interpellations[]` |
| `mo-questions` | ~2,500 | `/intrebare/<regnum>-<regdate>` | `qr.body.questions[]` |
| `mo-committee-meetings` | ~14,300 | `/comisie/<committee_id>/<date>` | `committee_synthesis.body.committees[].meetings[]` |
| `mo-reports` | 52 | `/raport/<issuing_body_id>/<year>` | `report_facsimile.body.report` |
| `mo-persons` | ~5,000 (post-bootstrap) | `/politicieni/<slug>` | `persons.json` registry (Q4) |

**Why multi-grain.** Five reasons:

1. **URL surface.** Each grain maps to a distinct URL pattern that SEO tools and the LLM agent both expect to address independently. A speech URL is canonical-different from its parent debate URL; both must exist.
2. **Per-grain mappings.** Speeches need a Romanian text analyzer over `text` + `dense_vector` for kNN; votes need numeric counts and outcome enums; reports need long-form headings indexed but compact metadata. One mapping per grain keeps each tight.
3. **Update blast radius.** When discourse-analysis enrichment ships, only `mo-speeches` reindexes. When the linker re-runs, only `mo-votes` (`defers_to`/`resolves` fields) reindexes. Coupled-grain anti-patterns (one giant index per MO with everything nested inside) would force whole-doc rewrites for any sub-record change.
4. **SEO crawl budget.** Search engines treat `monitorul.ai/discurs/...` and `monitorul.ai/comisie/...` as different content classes. Separate sitemaps per grain (Q7) let Google prioritize substantive content correctly.
5. **Aggregation queries.** Every researcher query in `discourse-analysis-schema.md` ("rank by populism", "filter by party-at-time") is naturally per-speech, terminating in `terms`-aggs over speech-grain fields. Per-doc indices would force `nested` aggs (slow, heap-hungry).

**Rejected: doc-grain only (one ES doc per MO sidecar).** Failure mode: a search for "find Iohannis quotes about NATO" returns a 370 KB document blob with no precise hit context; the public website becomes "one URL per MO issue" with sub-page navigation only via in-page anchors — non-shareable, poorly crawled, hostile to LLM-agent retrieval which thrives on small focused chunks.

**Rejected: ES `nested` with one doc per MO and 1M nested children.** Failure mode: nested updates require parent reindexing (each speech edit rewrites a 370 KB parent); nested aggs at this scale are heap-hungry and slow; result hits return parent-shaped JSON requiring app-side reshaping; `_search` payloads balloon. ES docs explicitly warn against high-cardinality nested fields.

**Rejected: parent/child mappings (`join` field type).** Failure mode: ES `join` mappings restrict parent-child to a single shard and disable many aggregations across the relationship; performance degrades nonlinearly with cardinality; the engineering complexity to gain parent-child semantics doesn't pay back when denormalized child-grain indices solve the same problem better.

### Q2. Stable identity for child records — persisted in sidecar

**Decision.** Every record across all grains has a canonical, **stable** `record_id` minted at extract time and persisted to the sidecar JSON in a new `extraction.identity` block (with its own `extractor_versions.identity` slot). The indexer copies `record_id` directly into ES `_id`. The same `record_id` is what URL slugs (Q7) and LLM-enrichment files (Q3) join on.

| Grain | record_id pattern | Source key |
|---|---|---|
| Document | `mo://YYYY/PART/ISSUE` | already in sidecar's `document_id` |
| Agenda item | `<doc_id>#agenda-<ordinal>` | SUMAR ordinal |
| Speech / activity | `<doc_id>#agenda-<ord>#act-<seq>` | source-order sequence within agenda |
| Vote | `<doc_id>#agenda-<ord>#vote-<seq>` | source-order sequence within agenda |
| Interpellation | `<doc_id>#interp-<interpellation_number>` when present, else `<doc_id>#interp-seq-<n>` | the `Nr. N/DD.MM.YYYY` natural key when extractable |
| Question | `<doc_id>#q-<regnum>` when present, else seq | the `regnum` natural key |
| Committee meeting | `<doc_id>#cmt-<committee_id>` | committee slug from registry |
| Report | `<doc_id>` | one report per R-MO |
| Person | `<person_id>` from `persons.json` | curated registry id (Q4) |

Each record also carries a `content_fingerprint`: sha256 of the normalized record content, truncated to 12 hex chars, stored alongside the `record_id` but **not** as the primary key. The fingerprint is forensic — when v0.3 of an extractor changes activity boundaries, fingerprints let migration scripts map old IDs to new ones for in-flight enrichments.

**Why persist in sidecar (not compute in indexer only).**

1. **One canonical key surface.** When the LLM enrichment system writes `<basename>.discourse.v0_2.json` keyed by `record_id`, it must derive the same key the indexer derives. If the keying logic only lives in the indexer, every consumer (enrichment agents, SQL exports, research notebooks, the website's webhook callbacks) re-implements it — a drift surface that grows with every consumer added.
2. **Re-extraction safety.** When v0.3 of an extractor reorganizes activities, comparing `content_fingerprint` between old and new sidecars lets a migration script say "old `act-12` and new `act-12` are the same speech (fingerprint matches) — keep the existing slug" vs "old `act-12` is gone (no fingerprint match in new) — retire the URL".
3. **Schema migration ergonomics.** When the ID strategy itself evolves (v2 adds a chunk index for long speeches), it's a sidecar backfill pass; the canonical SOT moves forward; ES rebuild follows.
4. **Defensibility.** "How was record `mo://2018/II/168#agenda-3#act-12` defined?" has a single deterministic answer in the sidecar, not a runtime lookup that may have changed since the URL was published.

**Rejected: indexer-only ID derivation, sidecar IDless.** Failure mode: every consumer re-implements the keying rules, drifts apart silently as edge cases (implicit-single-item agenda fallback, v0.2.9 collapsed-span recovery, future extractor evolutions) accumulate; LLM-enrichment files key against one set of IDs while ES uses another; cross-consumer joins start failing in subtle ways.

**Rejected: positional IDs without fingerprint.** Failure mode: extractor regex tweaks shift `source_span` offsets by a few characters — current behavior treats this as "same record" but a hash-of-offsets ID would change. URLs 404; LLM-enrichments orphan.

**Rejected: opaque hash-only short_ids in URLs.** Failure mode: human-debuggable URLs are a real ergonomic asset for journalists, researchers, and the LLM agent's tool tracing; opaque slugs erase that. Keep human-readable composite slugs (Q7) on top of the canonical record_id.

### Q3. Enrichment storage — parallel per-producer files + agent journal

**Decision.** Two kinds of LLM-derived data, two file conventions, both alongside the sidecar:

- **Batch enrichments** (topic classifier, NER, summaries, embeddings, discourse-analysis): `<basename>.<producer>.v<version>.json`. Top-level keyed by `record_id`. Each entry carries `_meta`: `{producer, version, model_id, prompt_hash, generated_at, source_sidecar_content_sha, confidence?}`. Versioned, blue-green replaceable. Multiple versions of the same producer can coexist (`topics.v0_1.json` next to `topics.v0_2.json`); indexer is configured which version is live.
- **Agent journal** (cached chat answers, user-triggered LLM tasks): `<basename>.journal.jsonl`. Append-only; one JSONL line per agent action: `{record_id, producer, timestamp, prompt_hash, payload}`. Never overwritten. Survives ES rebuild.

Indexer flow:

```
read sidecar  →  glob <basename>.*.json + .jsonl  →  merge by record_id  →  emit ES docs per grain
```

**Why parallel per-producer files.**

1. **Re-extraction never loses enrichments.** When extractor v0.3 ships, sidecar bodies rewrite; enrichment files are untouched; indexer's next pass merges them back in for free.
2. **Independent versioning.** Each producer rolls out and rolls back on its own clock. Schema bumps to `extraction_schema.json` happen rarely; prompt iterations on the topic classifier happen weekly. Decoupling them prevents one slowing the other.
3. **A/B-able prompts.** While tuning a discourse-analysis prompt, both v0.2 and v0.3 files live on disk; the indexer is flagged which one to project; flip in one config change.
4. **Defamation deletability.** If a court orders a populism-coding redacted, delete the offending entry from the per-producer file; sidecar canonical content is untouched; reindex propagates.
5. **Reproducible from disk.** Lose ES, the embedding service, and the LLM provider account — the corpus is still complete on disk + S3; rebuild ES overnight.

**Why a separate agent journal.** Agent runtime writes (cached "summarize this MO for me" answers, user-prompted "what did Politician X say about NATO in 2018") have a fundamentally different lifecycle than batch jobs: append-only, never invalidated by re-extraction (the cached answer was about that text at that moment), accumulating over time. Mixing them with versioned batch files would force per-message file rewrites, race conditions with batch reruns, and unclear retention semantics. JSONL append is race-free and dump-friendly.

**Rejected: enrichments inside sidecar `body`.** Failure mode: every experimental enrichment (topic prompt v3, embedding model bump, new discourse rubric) forces an `extraction_schema.json` bump and an `extractor_versions` slot — an order of magnitude more friction than the iteration speed needs. Re-extraction (which currently clobbers `body`) would also clobber enrichments unless complex merge logic is added; race conditions with agent runtime writes would corrupt the canonical record.

**Rejected: enrichments in a separate datastore (Postgres / SQLite / DuckDB).** Failure mode: another SOT to back up, another deploy surface, another network hop in the indexer; loses the "everything-on-S3-mirrored" simplicity that the rest of the pipeline already has. Filesystem + S3 is sufficient at the corpus's scale; enrichment file count tops out at ~50K (5552 docs × ~10 producers), trivially manageable.

**Rejected: ES as enrichment SOT.** Failure mode: ES becomes load-bearing for non-search-engine concerns; rebuilding ES becomes a data-loss event; the LLM agent's cached answers cannot be redacted without losing search capability.

### Q4. Speaker canonicalization — `persons.json` registry with hybrid bootstrap

**Decision.** Add `persons.json` as a fourth curated registry (alongside `institutional_bodies.json` and `ministries.json`), a `backfill_persons` pass (sister to `backfill_ministries`), and a `mo-persons` ES index. Every Speaker in every sidecar gets `person_id` populated from the registry; unresolved speakers stay `person_id: null` and surface in a coverage report for hand-resolution.

Registry shape:

```json
{
  "id": "iordache-florin",
  "canonical_name": "Florin Iordache",
  "diacritic_form": "Florin Iordache",
  "aliases": ["Iordache Florin", "F. Iordache", "Florin V. Iordache"],
  "wikidata_qid": "Q5460872",
  "birth_date": "1960-01-23",
  "mandates": [
    {"role": "deputat", "chamber": "Camera Deputaților", "legislature": "VII",
     "from": "2012-12-19", "to": "2016-12-21", "party": "PSD"},
    {"role": "ministru_justitiei", "from": "2017-01-04", "to": "2017-02-09"},
    {"role": "deputat", "chamber": "Camera Deputaților", "legislature": "VIII",
     "from": "2016-12-21", "to": "2020-12-21", "party": "PSD"}
  ],
  "homonym_disambiguation": null
}
```

**Bootstrap pipeline (one-time):**

1. Aggregate all distinct raw speaker strings from the corpus (~15K–25K distinct).
2. Cluster by name-form normalization (cedilla-fold + diacritic-strip + lowercase + token-set) → ~5K–8K cluster centroids.
3. Seed `persons.json` from authoritative public sources: cdep.ro per-legislature deputy lists, senat.ro senator lists, government composition, Wikidata for presidents/PMs/EP-members.
4. Match clusters → registry entries with the tiered `normalize_*` strategy already used in `monitorul_ii.registries`: exact → case-insensitive → diacritic-stripped → token-set → fuzzy (Levenshtein, justified for human names).
5. Hand-resolve the long-tail: clusters with no match, multi-match (homonyms), name-changes (married names). ~500–1500 manual decisions; tractable.

**Runtime resolution:** `backfill_persons` walks every Speaker, fills `person_id`, records `matched_via` for telemetry. Same Q11 contract as the linker (re-extract clobbers; re-running backfill recovers). Unresolved speakers surface in a coverage CLI output; missing entries get added to `persons.json` over time.

**Why curated registry, not LLM entity resolution.**

1. **Determinism.** Every match is rule-based and re-derivable. "How did `Domnul Iordache` resolve to `iordache-florin`?" has one answer with citations; a politician's lawyer cannot challenge the resolution chain.
2. **Replicability.** A research paper citing populism rankings must be replicable next year. LLM-driven resolution non-deterministically maps the same input to different IDs across model versions.
3. **Cost.** 1M speeches × $0.001 per LLM call = $1000 every time the matcher runs; a registry lookup is free.
4. **Defamation surface.** "Politician X said Y" is a libel-shaped claim if X was wrongly identified. Curated rules with audit trails are defensible; LLM judgments are not.

**Rejected: index speakers by name keyword only, no canonicalization.** Failure mode: every aggregation query the discourse-analysis layer enables ("rank politicians by populism") breaks across diacritic forms, abbreviations, mojibake. Politician pages on `monitorul.ai/politicieni/<slug>` are unbuildable without canonical IDs.

**Rejected: pure LLM entity resolution.** See above — non-determinism, cost, defamation exposure.

**Rejected: hybrid LLM-suggests-rule-confirms.** Failure mode: a viable middle path, but dominates only when the curated registry is too small to seed clustering. With cdep.ro / senat.ro / Wikidata as seed sources, the registry covers >95% of the corpus before any LLM is needed; the long-tail is small enough for human resolution.

### Q5. Content shape per ES doc — denormalized; full text; Romanian analyzer; flat refs

**Decision.** Each ES doc carries everything needed to render its page, run any aggregation in scope, and serve the LLM agent's tool calls — **without joining to other indices**. Parent context is denormalized down (chamber, session_date, agenda title, agenda category copied onto every speech doc). Full speech `text` is in ES (550 MB raw → ~2–3 GB indexed). References are pre-flattened from the source `references_mentioned[]` array into per-type keyword arrays. Romanian Snowball analyzer + asciifolding. `is_substantive: true` (text_length ≥ 100 chars) is the default filter on public search.

**Why denormalize.**

1. **Join-free queries.** ES doesn't do general-purpose joins efficiently; cross-index relational queries require app-side merging. Denormalization at index time turns every page render into a single ES query.
2. **Filter-by-everything.** Researcher and agent queries often filter by chamber+legislature+party+year+topic+ref simultaneously. With parent context denormalized, every filter is a `term`/`terms` clause on the speech doc — fast, cacheable, aggregation-friendly.
3. **Index size is fine.** Full text + denormalized parent context across 1M speech docs = ~3 GB. Trivial for self-hosted ES on commodity hardware.
4. **Reindex blast radius is bounded.** When parent context changes (e.g., the agenda extractor recategorizes an agenda item), all child speech docs are reindexed for that doc_id — a few hundred docs. Manageable.

**Why full text in ES.** BM25 cannot run against an S3 pointer. The text *is* the searchable content; semantic search and discourse-analysis aggregations both need it inline. The 550 MB raw text → ~2–3 GB indexed cost is well below the threshold where a lean-doc + S3-fetch architecture would be justified.

**Why Snowball Romanian + asciifolding.** ES's built-in `romanian` analyzer handles diacritic folding via `asciifolding` and stems noun/verb cases (`Iohannis/Iohannisului`, `lege/legii/legilor`). Pair with a `.exact` keyword subfield for phrase queries that must respect diacritics. There is no widely-used Romanian-specific custom analyzer worth shipping over Snowball Romanian for v1; UAIC's lemmatizer is heavier and gains marginal recall.

**Why flat refs (not `nested`).** ES `nested` is a sharp tool: each nested object becomes a Lucene doc, multiplying corpus cardinality. At 1M parents × ~5 refs/speech = 5M nested docs, heap pressure and slower aggregations. `nested` is required only when correlated multi-field queries within a single array element matter (e.g., "speeches where this same ref has type=bill AND prefix=PL-x"). Your queries are mostly "speeches mentioning bill PL-x 100/2018" — flat per-type keyword arrays (`refs.bills`, `refs.laws`, `refs.codes`) are faster and simpler. Reserve `nested` for `discourse.evidence` (where evidence object correlation matters: lines+framework+polarity together).

**Why `is_substantive` filter.** Live speech-length distribution across the indexed corpus (5,552 MOs, 817,483 speeches): p10 ≈ 27, p25 ≈ 67, p50 ≈ 198, p75 ≈ 749, p99 ≈ 6,070 chars. The p10–p25 band is dominated by chair-procedure phrases ("Mulțumesc, domnule deputat", "Vă rog."). Without the filter, ~282K of 817K speeches (34.6%) fall below the cutoff, so a public search returns roughly a third chair-procedure noise; SEO-wise, ~282K thin-content URLs would tank ranking signal across the domain. The filter is on by default for public search and indexability, off for admin and bulk export. **Why 100 specifically.** The cutoff sits between p25 (~67) and p50 (~198) of the unfiltered distribution — above the chair-phrase cluster but below the median substantive turn — so single-paragraph remarks aren't lost while one-line interjections are. Round-number convention: easier to communicate and reason about than 80 or 124, which would land in the same gap. The same constant lives in code as `SUBSTANTIVE_TEXT_LENGTH` (`elasticsearch/denormalize.py`) and `MIN_TEXT_CHARS` (`extraction/enrichments/embedding.py`); changing it is a coordinated bump, not a one-file edit.

**What stays out of ES.**

- The PDF and MD live in S3; ES holds only an `s3_url` pointer on `mo-documents`.
- `source_span` char/line offsets stay sidecar-only — they're a re-resolution artifact for highlighting, not a search field.
- `coverage` block is ops-only; surfaced on `mo-documents` only.
- Boilerplate-claimed text stays out of speech `text`.
- Tiny chair turns are indexed (so the LLM agent's full-corpus tools can hit them when needed) but excluded from the public default search via `is_substantive`.

**Rejected: lean docs with runtime joins.** Failure mode: every page render becomes a multi-query operation; aggregations across parent dimensions need join-or-denormalize-at-query-time logic; the LLM agent's tool implementations grow query-construction complexity for no storage win.

**Rejected: speech text in S3 with ES holding only a 1KB excerpt.** Failure mode: BM25 over 1KB excerpts misses 90% of the searchable signal; semantic search becomes unimplementable; "find every speech mentioning Codul muncii art. 65" returns false negatives whenever the cite is past the excerpt boundary.

**Rejected: embeddings inline in this section.** Embeddings have their own producer, lifecycle, and cost story; deferred to Q8.

### Q6. Index lifecycle — hybrid (in-place upsert routine + blue-green major) with SQLite state

**Decision.** Hybrid pattern with two operational modes:

- **Routine triggers** (1–6, 9): in-place upserts via per-grain write aliases. Daily MO ingestion, re-extraction, linker reruns, backfill reruns, enrichment producer version bumps, new enrichment producers, redactions all flow through `index <id>` API calls keyed by `record_id`.
- **Major triggers** (7, 8): blue-green via versioned indices. Mapping changes (new `dense_vector` dim, new analyzer config, new field type), schema breaking-changes (sidecar `1.12.0 → 1.13.0`) trigger creation of a new versioned index (`mo-speeches-20260615-v2`), parallel indexing from sidecars, dual-write during catch-up, atomic alias swap.

State tracking lives in `monitorul.db` SQLite:

```sql
CREATE TABLE es_indexed (
  document_id        TEXT PRIMARY KEY,
  sidecar_content_sha TEXT NOT NULL,
  enrichment_fingerprint TEXT NOT NULL,
  index_generation   TEXT NOT NULL,
  indexed_at         INTEGER NOT NULL,
  child_record_ids   TEXT NOT NULL  -- JSON array of record_ids derived from this sidecar
);
```

On every indexer pass, for each sidecar: compute `(sidecar_content_sha, enrichment_fingerprint)`. If matches the row, skip. If miss, derive new ES docs, upsert via write alias, **diff `child_record_ids` old vs new and `delete_by_query` orphans**, update the row. The orphan-delete step is critical for re-extraction (e.g., when v0.3 merges two adjacent speeches into one, old `act-12`/`act-13` must die in ES, not orphan).

CLI:

```
monitorul-ii index <path>...
  [--force] [--dry-run]
  [--target=<index-generation>]   # blue-green: write to a specific generation
  [--mirror]                       # write to live + target (swap-day catch-up)
  [--rebuild]                      # blue-green helper: zero-out target and rebuild
  [--grain=speeches|documents|...] # only one grain
  [--es-host=...]
```

**Why hybrid.**

1. **Routine = cheap.** Daily ingestion of 1–5 new MO sidecars produces a few hundred ES updates, all via in-place upsert. No alias-swap overhead, no storage 2x. The 99th-percentile operational case stays simple.
2. **Major = clean.** Mapping changes happen rarely (maybe quarterly) but must be zero-downtime. Blue-green guarantees that: build new generation, dual-write while building, validate, atomic swap, drop old. Rollback is a swap back.
3. **One indexer codepath.** The Python indexer does the same work in routine and rebuild modes — only `--target`/`--mirror` flags change. No bifurcated logic.
4. **State is local.** SQLite is millisecond-fast for the per-doc state lookup; no extra ES round-trip per indexer iteration.
5. **Orphan delete is explicit.** Storing `child_record_ids` lets the indexer compute "what did this sidecar produce last time vs this time" deterministically. No stale records linger.

**Refresh interval:** `30s` on routine indices (good search-after-write UX, reasonable merge pressure); `-1` (manual) during blue-green rebuild (lets bulk-load run fast; refresh once at end).

**Sharding & replicas:** 1 primary shard per index (corpus is small enough; simplifies cluster math); 1 replica for HA on a single-node ES this collapses to 0, on a multi-node it gives availability. Tune later if shard size exceeds 30 GB on any index.

**No ES ingest pipelines.** All normalization (slug generation, cedilla-fold, mojibake repair) happens in the Python indexer. Ingest pipelines duplicate logic and are debug-hostile.

**Rejected: always-in-place upserts.** Failure mode: mapping changes require either downtime (delete index, recreate, repopulate) or dual-write logic baked into every write path. Schema bumps become major events, not routine.

**Rejected: always-blue-green.** Failure mode: 5× storage overhead during routine daily ingestion; alias-swap latency per ingestion cycle; operational ceremony for what should be a 30-second cron.

**Rejected: ES-side state (query before write).** Failure mode: every indexer iteration round-trips to ES to fetch `(content_sha, enrichment_fingerprint)`, doubling network cost; no obvious latency win over SQLite; less debuggable.

### Q7. SEO surface — slug-once, indexability cutoff, sitemap index, JSON-LD, Wikidata

**Decision.** Six coupled SEO commitments, anchored on URL stability:

**Slug stability.** Slugs are minted once per record on first indexer pass and persisted to the sidecar at `extraction.identity.slug`. Subsequent passes reuse the existing slug. Format: `<title-derived-keywords>-<short_id>` where the keywords are decorative (5–8 ASCII-folded words from the first canonical title) and the trailing short_id is the canonical identifier (base32-encoded composite of `(year, issue, ordinal, seq)`, ~10 chars). Server route matches on the short_id only — renamed slug variants 301-redirect to canonical.

**Indexability rules.**

| Grain | `<meta robots>` |
|---|---|
| `mo-documents` | `index, follow` |
| `mo-agenda-items` | `index, follow` |
| `mo-speeches` `is_substantive: true` | `index, follow` |
| `mo-speeches` `is_substantive: false` | `noindex, follow` |
| `mo-votes` | `index, follow` |
| `mo-interpellations` | `index, follow` |
| `mo-questions` | `index, follow` |
| `mo-committee-meetings` | `index, follow` |
| `mo-reports` | `index, follow` |
| `mo-persons` | `index, follow` |
| Search results | `noindex, follow` |
| Faceted/filter URLs | canonical to base + `noindex` if combinatorial |

Estimated indexable URL count: ~640K — sizable but Google-tractable. Without `is_substantive` filter, ~1.1M with 40% thin-content; domain-wide ranking risk.

**Sitemaps.** Sitemap index at `/sitemap.xml`; per-grain time-partitioned children (`sitemap-documents-2018.xml.gz`, `sitemap-speeches-2018-11.xml.gz`, `sitemap-persons.xml.gz`). 50K URLs max per file (Google's hard limit). `<lastmod>` = ES doc's `indexed_at`. Generated nightly via `monitorul-ii sitemap`, written to S3, served as static.

**JSON-LD.** Every page emits structured data in `<head>`:

- Document → `Article` + `GovernmentService`
- Agenda item → `Article` + `about: {Legislation}` for bill cites
- Speech → `Quotation` + `Person` (speaker, with `sameAs: <wikidata_qid_url>`) + `isPartOf` (parent doc)
- Person → `Person` with `affiliation`, `memberOf`, `jobTitle`, `sameAs: wikidata_qid`
- Committee meeting → `Event` with `attendee` list

**Canonical handling.** Every page emits `<link rel="canonical">` to its slug-form URL. Re-extraction → same slug → same canonical → no Google churn. Filter overlays canonical to base.

**Wikidata QIDs.** `persons.json` registry seeds each entry with a Wikidata QID where one exists. Knowledge-panel candidacy + cross-language entity resolution.

**Why slug-once.** Without persistence, indexer rule changes (e.g., better keyword extraction in v0.2) regenerate slugs on the next pass; Google sees URL drift; rankings reset. Storing the first-mint slug locks it in forever.

**Why short_id at slug tail.** Allows the human-readable part to evolve without breaking the URL; the server matches the short_id only.

**Why ASCII-folded slugs (not Romanian-with-diacritics).** Google handles both, but ASCII URLs survive SMS/chat/email transport without IRI encoding artifacts; Romanian diacritics live in `<title>`, `<h1>`, body content, and JSON-LD where they belong.

**Why `is_substantive` cutoff.** A sub-100-char chair turn ("Mulțumesc, domnule deputat. Vă rog.") has no content for Google to rank; indexing ~282K such pages dilutes the substantive corpus's ranking signal. Keep them in ES (LLM agent might need them); exclude from public crawlable surface.

**Rejected: slugs computed live in indexer with no persistence.** Failure mode: any indexer rule change → slug drift → URL churn → SEO collapse on already-published content.

**Rejected: opaque hash slugs only.** Failure mode: human-debuggability disappears; researchers can't eyeball a URL to know what record it points to; agent tool tracing becomes noisier.

**Rejected: index everything regardless of substance.** Failure mode: ~282K thin-content pages flag the whole domain; substantive content's ranking degrades.

### Q8. Embeddings — BGE-M3 1024-dim local, hybrid BM25+kNN via RRF

**Decision.** A new enrichment producer (`embedding`) generates per-record `dense_vector` embeddings using **BGE-M3 (1024 dim) running locally**, persisted as `<basename>.embedding.bge-m3.v0_1.json` files keyed by `record_id`. The indexer projects vectors into ES `dense_vector` fields. Search combines BM25 + kNN via Elasticsearch's native Reciprocal Rank Fusion (RRF, ES 8.9+).

**Granularity.**

| Grain | Embedded? |
|---|---|
| `mo-speeches` `is_substantive: true` (~500K) | ✅ |
| `mo-speeches` non-substantive (~450K) | ❌ — embed identically; pollute kNN |
| `mo-agenda-items` (title) | ✅ |
| `mo-documents` | ❌ — whole-doc embedding loses signal |
| `mo-persons` | ❌ pre-computed; query-time centroid only — defamation surface |
| `mo-committee-meetings` | ✅ (purpose + agenda items) |
| `mo-interpellations` | ✅ (question_text + topic) |
| `mo-questions` | ✅ |
| `mo-reports` | ✅ (title + headings) |

Long-tail speeches >8K tokens get chunked into 2K-token windows in v0.2 (one vector per chunk under `record_id#chunk-N`); v0.1 uses first-2K-tokens-only.

**Hybrid contract.** Every embedding entry stores a `text_fingerprint` alongside the vector. On indexer pass, if `text_fingerprint` ≠ current normalized text → vector is stale, excluded from kNN until re-embedded. BM25 on the doc still works. No risk of serving a vector that doesn't match its text.

**Why BGE-M3 local (1024-dim).**

1. **Free at re-embed time.** Switching models or bumping versions costs zero per-token spend.
2. **Romanian quality.** MTEB benchmarks place BGE-M3 on par with OpenAI-3-large on multilingual tasks; well ahead of OpenAI-3-small on non-English.
3. **Privacy.** Romanian political content stays in-house. Defamation cases don't have to argue about third-party data flow.
4. **Operational fit.** Self-hosted ES already implies on-premises infrastructure. Adding a sentence-transformers + FastAPI service on the same box is a few-hour task.
5. **Storage.** 1024-dim is the sweet spot — 4 GB raw vectors + ~6 GB HNSW. Comfortable on a 32 GB ES box.

**Alternatives considered (kept here so future-you can revisit):**

| Option | Dim | Cost (948K) | Romanian | Pros | Cons |
|---|---|---|---|---|---|
| **BGE-M3 (chosen)** | 1024 | $0 | Strong | Free, private, on-prem | Need to run a Python embedder service |
| OpenAI `text-embedding-3-small` | 1536 | ~$10–15 | Decent | Simplest API | Vendor lock-in; data over wire; per-token cost on re-embed |
| Cohere `embed-multilingual-v3` | 1024 | ~$30 | Strong on Romanian | Strong quality | Same lock-in/cost concerns |
| Voyage `voyage-3-large` | 1024 | ~$50 | Strong | Strong quality | Same |
| OpenAI `text-embedding-3-large` | 3072 | ~$50 | Strong | Strong quality | 3× ES storage cost; same lock-in |
| E5-large multilingual (open) | 1024 | $0 | Strong | Free, on-prem | Slightly weaker than BGE-M3 on MTEB-romanian |

**Hybrid search via RRF.** ES 8.9+ has native `rank.rrf` / `retrievers.rrf`. Standard pattern: BM25 leg (matches on `text` + `agenda_title` + speaker) + kNN leg (vector similarity on `embedding`); RRF merges with one tunable hyperparameter (`rank_constant`, default 60). BM25 wins on rare exact terms (bill numbers, proper nouns); kNN wins on synonyms/paraphrasing. RRF lets each leg contribute where it's strong without manual weighting.

> **Implementation note (post-design).** The native `retrievers.rrf` DSL is gated behind a Platinum+ license; basic-tier clusters return `403 / current license is non-compliant for [Reciprocal Rank Fusion (RRF)]`. We ship against basic, so the actual implementation in `monitorul_ii.elasticsearch.queries._search_speeches_rrf` issues BM25 + kNN as two separate `_search` calls and fuses them in Python via `_fuse_rrf_legs` using the same `score(d) = Σ 1/(rank_constant + rank_in_leg)` formula. Two ES round-trips per query rather than one; the latency penalty is negligible at our QPS, and the fusion math is identical. License-tier portability is the right invariant for an open-data project. If we later move to a Platinum cluster the native path can return behind a feature flag, but the client-side path stays the default.

**Operational notes.**

- Embedding service: sentence-transformers + BGE-M3 in FastAPI; batch size 32; GPU optional (~10× faster) but CPU works for the bootstrap.
- Bootstrap: ~3 hours on a single consumer GPU, ~30 hours on CPU. One-time.
- Mapping: `embedding: {type: dense_vector, dims: 1024, index: true, similarity: cosine}` + HNSW defaults (`m: 16, ef_construction: 100`).
- Re-embedding triggered by `text_fingerprint` mismatch only; never recompute unchanged vectors.

**Rejected: kNN-only.** Failure mode: rare exact-term queries (bill cites, proper nouns) underperform — the embedding's compression loses the signal. Hybrid is the standard now; ES's native RRF makes the cost trivial.

**Rejected: cloud API embeddings.** See alternatives table — operational fit favors local for this project.

**Rejected: per-document whole-text embeddings.** Failure mode: whole-doc embeddings lose locality; an MO with 100 agenda items has each item's signal averaged; semantic search returns the right MO but not the right speech within it. Per-record embeddings give the right resolution.

**Rejected: pre-computed person centroids.** Failure mode: storing "Politician X's mean populism vector" turns a per-speech-act layer into a personality attribution layer — exactly the libel surface the discourse-analysis schema is designed to avoid.

### Q9. Next.js ↔ ES interface — typed `lib/search.ts` query layer

**Decision.** Next.js server-side hits ES through a typed query layer (`lib/search.ts`) of 8–12 functions. The layer is the only path from app code to ES. The LLM agent's tool implementations wrap the same functions. Public traffic gets read-only ES credentials (`monitorul_reader`). Cluster-level guards on aggregation buckets and bool query clauses.

```ts
// lib/search.ts — server-only; never bundled to the client
export async function searchSpeeches(params: {
  q?: string;
  speakerPersonId?: string;
  chamber?: 'Camera Deputaților' | 'Senat';
  dateFrom?: string; dateTo?: string;
  refBills?: string[];
  topics?: string[];
  isSubstantive?: boolean;        // default true on public; false on admin
  page?: number; pageSize?: number; // hard cap pageSize=50
  rankFusion?: 'rrf' | 'bm25-only';
}): Promise<SearchResult<Speech>>;

export async function getDocument(id: string): Promise<Document | null>;
export async function listDocumentsByDate(date: string, chamber?: string): Promise<Document[]>;
export async function getAgendaItem(id: string): Promise<AgendaItem | null>;
export async function getSpeech(id: string): Promise<Speech | null>;
export async function personPage(slug: string): Promise<{ person: Person; recentSpeeches: Speech[]; stats: PersonStats }>;
export async function searchPersons(q: string): Promise<Person[]>;
export async function listCommitteeMeetings(committeeId: string, dateFrom?: string): Promise<CommitteeMeeting[]>;
export async function getReport(id: string): Promise<Report | null>;
// 8-12 total
```

**ES credentials & network.**

- `monitorul_reader`: read-only role on `mo-*` indices, no scripting, no scroll, no `_sql`, no cluster info. Used by Next.js.
- `monitorul_indexer`: read+write on `mo-*` indices, no cluster admin. Used by `monitorul-ii index`.
- ES bound to private network only; no public port.
- Cluster guards: `search.max_buckets: 65536`; `indices.query.bool.max_clause_count: 1024`.

**Caching.**

- Document/agenda/speech/person/committee/interpellation/question/report pages → `force-static` with ISR `revalidate: 3600` + `revalidateTag('mo-<grain>:<id>')` invalidation called by indexer webhook.
- Search results → `Cache-Control: s-maxage=60, stale-while-revalidate=300`.
- Sitemaps → `force-static`, regenerated nightly to S3.
- Indexer's `monitorul-ii index` calls a Next.js webhook on every successful upsert to trigger ISR invalidation for the affected pages.

**Rate limits & abuse.**

- Public search: 60 req/min per IP via Next.js middleware.
- LLM agent tools: max 30 ES calls per agent turn, enforced in tool wrapper.
- All queries logged to a `mo_query_log` index for cost analysis and abuse detection.
- Query-log observability is versioned under `kibana/dashboards/query-log-overview.json` and deployed with `scripts/kibana_dashboards.py upsert` on Kibana 9.4+. The dashboard targets `QUERY_LOG_INDEX` and expects the web app query logger to emit the small operational field set needed for abuse/cost work: `timestamp`, `op`, `took_ms`, `es_took_ms`, `error`, `hits_total`, `surface`, and served retrieval `mode`. Field-name overrides are explicit env vars so the dashboard can survive a web-app logging rename without editing panel JSON.

**Rejected: direct ES client in route handlers.** Failure mode: every route handler builds its own query; cost-runaway and injection-hardening are ad-hoc; no central enforcement of `pageSize` caps or filter constraints.

**Rejected: ES Search Templates.** Failure mode: indirection between two repos (template definitions in ES, callers in Next.js); harder to type-check; ceremony that pays back only when many heterogeneous clients share one ES — single-team self-hosted is below that threshold.

**Rejected: separate API gateway service.** Failure mode: deploy surface for a v1 that doesn't have external API consumers; defer until external researcher API is concretely needed.

## Consolidated index mappings (v1)

The following mappings are the full v1 surface. Field-level rationale lives in Q5; this section is the authoritative reference.

### Common fields (every grain)

```jsonc
{
  "record_id":               "keyword",
  "document_id":             "keyword",
  "content_fingerprint":     "keyword",
  "content_sha_source":      "keyword",
  "indexed_at":              "date",
  "extractor_versions":      { "type": "object", "dynamic": true },
  "enrichment_versions":     { "type": "object", "dynamic": true },
  "schema_version":          "keyword"
}
```

### `mo-speeches`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "session_date":            "date",
  "session_type":            "keyword",
  "legislature":             "keyword",
  "year":                    "short",
  "mo_issue":                "keyword",
  "agenda_ordinal":          "short",
  "agenda_title":            { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": { "type": "keyword", "ignore_above": 512 } } },
  "agenda_category":         "keyword",
  "agenda_outcome":          "keyword",

  "speaker": {
    "person_id":             "keyword",
    "name_raw":              "keyword",
    "name_search":           { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": { "type": "keyword" },
                                           "folded":  { "type": "text", "analyzer": "romanian_folded" } } },
    "title":                 "keyword",
    "role":                  "keyword",
    "party_group_at_time":   "keyword",
    "delivery_mode":         "keyword"
  },

  "text":                    { "type": "text", "analyzer": "romanian",
                               "fields": { "exact": { "type": "text", "analyzer": "romanian_exact" } } },
  "text_length":             "integer",
  "is_substantive":          "boolean",
  "position_in_agenda":      "integer",

  "refs": {
    "types":                 "keyword",
    "bills":                 "keyword",
    "laws":                  "keyword",
    "codes":                 "keyword",
    "ougs":                  "keyword",
    "ogs":                   "keyword",
    "raw":                   "keyword"
  },

  "enrichments": {
    "topics":                "keyword",
    "summary":               { "type": "text", "analyzer": "romanian" },
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" },
    "embedding_text_fingerprint": "keyword",
    "discourse": {
      "frameworks":          "keyword",
      "populism_hawkins": {
        "score":             "byte",
        "evidence": {
          "type": "nested",
          "properties": {
            "lines":         "integer_range",
            "quote_excerpt": { "type": "text", "analyzer": "romanian" }
          }
        }
      }
      // ... other framework codings (DQI, V-Party, CMP, CHES, custom Romanian markers)
    }
  },

  "slug":                    "keyword",
  "url_path":                "keyword"
}
```

### `mo-documents`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "part":                    "keyword",
  "issue":                   "keyword",
  "year":                    "short",
  "published":               "date",
  "session_date":            "date",
  "session_type":            "keyword",
  "legislature":             "keyword",
  "document_type":           "keyword",
  "title":                   { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": { "type": "keyword", "ignore_above": 512 } } },

  "summary":                 { "type": "text", "analyzer": "romanian" },
  "agenda_count":            "integer",
  "speech_count":            "integer",
  "vote_count":              "integer",
  "interpellation_count":    "integer",
  "question_count":          "integer",
  "committee_count":         "integer",
  "report_count":            "integer",

  "coverage": {
    "claimed_pct":           "float",
    "body_chars":            "integer"
  },

  "s3_url_pdf":              "keyword",
  "s3_url_md":               "keyword",
  "s3_url_sidecar":          "keyword",

  "slug":                    "keyword",
  "url_path":                "keyword"
}
```

### `mo-agenda-items`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "session_date":            "date",
  "legislature":             "keyword",
  "ordinal":                 "short",
  "category":                "keyword",
  "title":                   { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": { "type": "keyword", "ignore_above": 512 } } },
  "outcome":                 "keyword",
  "confidence_type":         "keyword",
  "requested_by_group":      "keyword",
  "reexamination_reason":    "keyword",
  "topics_primary":          "keyword",

  "refs": {
    "types":                 "keyword",
    "bills":                 "keyword",
    "laws":                  "keyword",
    "codes":                 "keyword",
    "raw":                   "keyword"
  },

  "speaker_person_ids":      "keyword",
  "vote_summary": {
    "total_votes":           "integer",
    "outcomes":              "keyword"
  },

  "enrichments": {
    "summary":               { "type": "text", "analyzer": "romanian" },
    "topics":                "keyword",
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" }
  },

  "slug":                    "keyword",
  "url_path":                "keyword"
}
```

### `mo-votes`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "session_date":            "date",
  "legislature":             "keyword",
  "agenda_ordinal":          "short",
  "agenda_title":            { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword" } },
  "agenda_category":         "keyword",

  "motion_type":             "keyword",
  "voting_method":           "keyword",
  "outcome":                 "keyword",

  "counts": {
    "for":                   "integer",
    "for_unanimous":         "boolean",
    "against":               "integer",
    "abstain":               "integer",
    "not_voting":            "integer",
    "total":                 "integer"
  },

  "quorum_met":              "boolean",
  "deferred":                "boolean",
  "defers_to":               "keyword",
  "resolves":                "keyword",

  "refs": {
    "types":                 "keyword",
    "bills":                 "keyword",
    "laws":                  "keyword"
  },

  "proposed_by": {
    "person_id":             "keyword",
    "name":                  "keyword",
    "is_government":         "boolean"
  },

  "url_path":                "keyword"
}
```

### `mo-interpellations`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "session_date":            "date",
  "legislature":             "keyword",
  "interpellation_number":   "keyword",

  "questioner": {
    "person_id":             "keyword",
    "name":                  "keyword",
    "party_group_at_time":   "keyword"
  },
  "addressed_to":            "keyword",
  "addressed_to_normalized": "keyword",

  "topic":                   { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword" } },
  "question_text":           { "type": "text", "analyzer": "romanian" },
  "response": {
    "speaker": {
      "person_id":           "keyword",
      "name":                "keyword"
    },
    "text":                  { "type": "text", "analyzer": "romanian" }
  },
  "response_deferred":       "boolean",
  "genre":                   "keyword",
  "delivery_mode":           "keyword",

  "enrichments": {
    "topics":                "keyword",
    "summary":               { "type": "text", "analyzer": "romanian" },
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" }
  },

  "slug":                    "keyword",
  "url_path":                "keyword"
}
```

### `mo-questions`

```jsonc
{
  // ... common fields
  "chamber":                 "keyword",
  "regnum":                  "keyword",
  "regdate":                 "date",
  "questioner": {
    "person_id":             "keyword",
    "name":                  "keyword",
    "party_group_at_time":   "keyword"
  },
  "addressee": {
    "raw":                   "keyword",
    "ministry_normalized":   "keyword",
    "institutional_normalized": "keyword"
  },
  "topic":                   { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword" } },
  "text":                    { "type": "text", "analyzer": "romanian" },
  "delivery_mode":           "keyword",

  "enrichments": {
    "topics":                "keyword",
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" }
  },

  "url_path":                "keyword"
}
```

### `mo-committee-meetings`

```jsonc
{
  // ... common fields
  "committee_id":            "keyword",
  "committee_name":          { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword" } },
  "committee_kind":          "keyword",
  "joint_with":              "keyword",

  "meeting_date":            "date",
  "format":                  "keyword",
  "purpose":                 { "type": "text", "analyzer": "romanian" },

  "agenda_items": {
    "type": "nested",
    "properties": {
      "ordinal":             "short",
      "title":               { "type": "text", "analyzer": "romanian" },
      "role":                "keyword",
      "outcome":             "keyword",
      "outcome_text":        { "type": "text", "analyzer": "romanian" },
      "primary_references":  "keyword"
    }
  },

  "roster": {
    "type": "nested",
    "properties": {
      "person_id":           "keyword",
      "name":                "keyword",
      "status":              "keyword",
      "role":                "keyword",
      "party_group_at_time": "keyword"
    }
  },

  "signatures": {
    "president_person_id":   "keyword",
    "secretary_person_id":   "keyword"
  },

  "enrichments": {
    "summary":               { "type": "text", "analyzer": "romanian" },
    "topics":                "keyword",
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" }
  },

  "url_path":                "keyword"
}
```

### `mo-reports`

```jsonc
{
  // ... common fields
  "issuing_body":            "keyword",
  "issuing_body_normalized": "keyword",
  "title":                   { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword" } },
  "reporting_period": {
    "from":                  "date",
    "to":                    "date"
  },
  "received_at": {
    "session_date":          "date",
    "session_kind":          "keyword",
    "received_in_document":  "keyword"
  },
  "headings": {
    "type": "nested",
    "properties": {
      "level":               "byte",
      "text":                { "type": "text", "analyzer": "romanian" }
    }
  },

  "enrichments": {
    "summary":               { "type": "text", "analyzer": "romanian" },
    "topics":                "keyword",
    "embedding":             { "type": "dense_vector", "dims": 1024,
                               "index": true, "similarity": "cosine" }
  },

  "url_path":                "keyword"
}
```

### `mo-persons`

```jsonc
{
  "_id":                     "<person_id>",
  "id":                      "keyword",
  "canonical_name":          { "type": "text", "analyzer": "romanian",
                               "fields": { "keyword": "keyword",
                                           "folded": { "type": "text", "analyzer": "romanian_folded" } } },
  "diacritic_form":          "keyword",
  "aliases":                 "keyword",
  "wikidata_qid":            "keyword",
  "birth_date":              "date",
  "mandates": {
    "type": "nested",
    "properties": {
      "role":                "keyword",
      "chamber":             "keyword",
      "legislature":         "keyword",
      "from":                "date",
      "to":                  "date",
      "party":               "keyword"
    }
  },
  "homonym_disambiguation":  "keyword",
  "slug":                    "keyword",
  "url_path":                "keyword",

  // computed periodically by a separate aggregation job — NOT pre-computed at write time
  "stats": {
    "speech_count":          "integer",
    "first_speech_date":     "date",
    "last_speech_date":      "date",
    "interpellation_count":  "integer",
    "question_count":        "integer"
  }
}
```

## Custom analyzers

```jsonc
{
  "settings": {
    "analysis": {
      "analyzer": {
        "romanian_folded": {
          "tokenizer": "standard",
          "filter": ["lowercase", "asciifolding"]
        },
        "romanian_exact": {
          "tokenizer": "standard",
          "filter": ["lowercase"]
        }
      }
    }
  }
}
```

`romanian` is ES's built-in (Snowball stemmer + asciifolding); `romanian_folded` is a stemming-free fold for diacritic-insensitive keyword matching; `romanian_exact` preserves diacritics for phrase queries that must match accurately.

## Operational appendix

### Pipeline order

```
fetch  →  convert  →  extract  →  link  →  backfill  →  enrich  →  index  →  sitemap
                                  ↑           ↑          ↑         ↑
                                  └───────────┴──────────┴─────────┘
                                       all idempotent, all version-aware,
                                       all re-runnable independently
```

### Daily cron

```
00:30  monitorul-ii fetch <today>            # 1-5 new MOs
00:35  monitorul-ii convert pdfs/<today>*    # PDF -> MD
00:40  monitorul-ii extract pdfs/<today>*    # MD -> sidecar
00:42  monitorul-ii link pdfs/<today>*       # cross-doc + intra-doc linkers
00:43  monitorul-ii backfill pdfs/<today>*   # registry-driven *_normalized
00:45  monitorul-ii enrich pdfs/<today>*     # enrichment producers (topics, embeddings, etc.)
00:55  monitorul-ii index pdfs/<today>*      # ES upsert
03:00  monitorul-ii sitemap                  # nightly sitemap regeneration to S3
```

### Bootstrap order (one-time)

1. Build `persons.json` registry (Q4). Hybrid: aggregate-cluster + cdep.ro/senat.ro/Wikidata seed + manual long-tail. ~1–2 weeks.
2. Add `extraction.identity` block + `slug` minting to extractor; backfill across the 5552 sidecars. ~1 week.
3. Build the BGE-M3 embedding service + `monitorul-ii embed` subcommand; embed substantive speeches + agenda items + meetings + interpellations + questions + reports. ~3 hours on GPU, ~30 hours on CPU.
4. Stand up ES with v1 mappings; run `monitorul-ii index --rebuild` against full corpus to populate. ~1 hour for 1M docs on commodity hardware.
5. Wire Next.js `lib/search.ts` against the populated indices; test page rendering, JSON-LD, sitemaps.
6. Deploy `monitorul.ai`; submit sitemap to Google Search Console.

### Disaster recovery

- ES is derived. Lose it → re-run `monitorul-ii index --rebuild` from sidecars + enrichments.
- Sidecars are derived from MD + canonical extractors. Lose them → re-run `monitorul-ii extract`.
- MDs are derived from PDFs. Lose them → re-run `monitorul-ii convert`.
- PDFs are mirrored to S3 + held by monitoruloficial.ro upstream. Lose local → fetch from S3 or re-run `monitorul-ii fetch`.
- Enrichments are derived from sidecars + an LLM provider OR a local model. Lose them → re-run the enrichment producers; results are non-deterministic but stable within a producer version.
- The agent journal is append-only and only on disk + S3. Treat it as the single non-recoverable layer; back up `<basename>.journal.jsonl` independently.

### Known caveats — read before iterating

- The `is_substantive: true` cutoff at 100 chars is a length proxy for "substantive content". A more precise filter (role-based: speeches by deputies/senators/ministers/PM/President, excluding chair-procedure turns) is preferable but requires the `persons.json` registry to be mature. Revisit once registry coverage exceeds ~95%.
- Q8 v0.1 embeddings use first-2K-tokens-only for speeches >8K tokens. The ~2% long-tail is under-represented in semantic search until v0.2 ships chunking.
- Q4 person canonicalization will under-resolve in early production (~80% expected at first index). Unresolved speakers stay `person_id: null`; politician pages are buildable for resolved-only entries; aggregations correctly exclude unresolved speeches (they're returned in raw search but not in person-rollup queries).
- The `mo-persons` `stats` block is computed by a separate periodic aggregation job, not at write time. There is intentionally no "discourse density score" pre-computed on persons — that's a query-time view per the discourse-analysis-schema mission constraints.
- The cross-reference linker (xref_linker v0.1.0) resolves only same-list `art. N` references. Cross-list cases (agenda anchor → speech-level `art. N`) are deferred to v0.2; ES indexes the unresolved unknowns as-is.
