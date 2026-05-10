# Discourse fields × semantic search

How the four-prompt discourse-analysis output (Hawkins / voice / DQI / V-Party) composes with the BGE-M3 semantic-search infrastructure to power the journalist UI, the LLM-agent layer, and the public API.

> Sister doc to:
> - `docs/discourse-pilot-baseline-2026-05.md` — calibration sweeps, model selection, four-cell H × V design.
> - `docs/discourse-analysis-schema.md` — schema decisions Q3/Q4/Q5/Q6/Q11.
> - `docs/elasticsearch-indexing.md` — Q3 (enrichment producer model), Q5 (denormalisation rules), Q9 (typed query layer).
> - `docs/architecture.md` § *Discourse-analysis producer* — implementation details.

## TL;DR

The discourse fields are **categorical metadata**, not embeddings. They don't power semantic similarity — that's BGE-M3's job. They compose orthogonally with semantic search as **filters**, **sort keys**, and **aggregation buckets**. Together they enable retrieval patterns no single primitive supports alone: "populist speeches about corruption", "rhetorically anti-pluralist speeches similar to this one", "the highest-DQI debate of 2024".

## The four retrieval primitives

The system exposes four distinct retrieval primitives. Understanding which one to reach for is the load-bearing operator skill.

| Primitive | What it matches | Field | Example query |
|---|---|---|---|
| **Lexical (BM25)** | exact words / stems | `text` (analyzer: `romanian`) + `text.folded` (`romanian_folded`) | "DNA" finds speeches mentioning "DNA" |
| **Semantic (kNN)** | meaning via 1024-dim vector cosine | `enrichments.embedding` (dense_vector) | "abuz de putere" finds speeches about graft, even without that exact phrase |
| **Hybrid (RRF)** | both, fused client-side | BM25 + kNN, fused via `Σ 1/(60+rank)` | best general-purpose ranking |
| **Categorical filter** | scalar / keyword exact match | `enrichments.discourse.*`, `chamber`, `session_date`, etc. | "all H=2 speeches" / "speeches in 2024" |

Each primitive answers a different question:

- **BM25**: "what speeches mention this word?"
- **kNN**: "what speeches are *about* this topic?"
- **RRF**: "what speeches are *most relevant* to this query?"
- **Filter**: "narrow the search to speeches with these properties"

The discourse fields populate the **filter** primitive. They never replace semantic search — they restrict, sort, or group its results.

## How discourse fields integrate

The denormaliser projects the discourse producer's output onto every speech document in `mo-speeches`:

```json
{
  "_id": "mo://2025/II/119#agenda-1#act-134",
  "_source": {
    "text": "Corupția din justiție...",
    "speaker": {"name": "Diana Șoșoacă", "person_id": "sosoaca-diana", ...},
    "session_date": "2025-04-15",
    "enrichments": {
      "embedding": [0.0123, -0.0456, ...],   // 1024 floats — semantic
      "discourse": {                           // aggregates — filter/agg/sort
        "hawkins": {
          "score": 2,
          "framework_confidence": 0.85,
          "framework_version": "hawkins@2018",
          "marker_count": 4,
          "marker_kinds": ["people_vs_elite", "evil_elite", "moralistic_manichaeism"],
          "rationale": "Discursul prezintă elemente populiste accentuate.",
          "markers": [                         // per-marker — render + highlight
            {
              "kind": "people_vs_elite",
              "marker_confidence": 0.9,
              "rationale_short": "Construct people-vs-elite explicit.",
              "evidence": {
                "text": "corupția din justiție",
                "char_range": [12, 33]         // slice into speech.text — highlight
              }
            },
            ...
          ]
        },
        "vparty": {
          "score": 2,
          "framework_confidence": 0.90,
          "framework_version": "vparty@2020 + vdem-attacks-on@v13",
          "marker_count": 3,
          "marker_kinds": ["judiciary_attack", "opposition_delegitimization"],
          "rationale": "Atacuri repetate la adresa instituțiilor.",
          "markers": [...]                     // same per-marker shape as hawkins
        },
        "voice": {
          "dominant_voice": "speaker_first_person",
          "voices_seen": ["speaker_first_person", "quoted"],
          "classifications": [                 // per-classification — link to marker_id
            {
              "marker_id": "m_0",
              "voice": "speaker_first_person",
              "voice_confidence": 0.95,
              "attributed_to": null,
              "rationale_short": "Speaker speaks in their own voice.",
              "voice_evidence": {"text": "corupția din justiție", "char_range": [12, 33]}
            },
            ...
          ]
        },
        "dqi": {
          "level_of_justification": 0,
          "content_of_justification": "none",
          "respect_for_groups": 0,
          "respect_for_demands": 0,
          "respect_for_counterarguments": 0,
          "constructive_politics": "positional",
          "framework_confidence": 0.8,
          "framework_version": "steiner-bachtiger@2017",
          "rationale": "Discurs strict pozițional, fără justificare.",
          "markers": [                         // value coerced to string for keyword mapping
            {"kind": "level_of_justification", "value": "0", "marker_confidence": 0.9, ...},
            {"kind": "content_of_justification", "value": "none", ...}
          ]
        }
      },
      "discourse_producer": "flash-lite",
      "discourse_text_fingerprint": "abc123def456"
    }
  }
}
```

The two layers occupy the same `enrichments` namespace but serve different operators:

- **`enrichments.embedding`** — used by `kNN` blocks in ES query DSL. Indexed with `similarity: cosine`, scored at query time.
- **`enrichments.discourse.*`** — used by `term`, `terms`, `range`, `exists` filters. Mapped as `byte` (scores), `keyword` (kinds, voices, DQI categoricals), or `float` (confidence). Cheap to filter on; cheap to aggregate on.

ES treats them as fully orthogonal. The query DSL composes them in one bool clause.

## Query patterns

The seven canonical patterns. All composable; the actual UI surfaces are combinations of these.

### Pattern 1 — Pure semantic search (no discourse)

Find speeches semantically similar to a topic, no register filter:

```python
search_speeches(q="reforma justiției", rank_fusion="rrf")
```

ES DSL (the kNN leg of the RRF pair):
```json
{
  "knn": {
    "field": "enrichments.embedding",
    "query_vector": [...],
    "k": 20,
    "num_candidates": 200
  }
}
```

This is the baseline semantic search. Discourse fields are absent from the query. Result ranking is pure cosine similarity (with BM25 fused via RRF).

### Pattern 2 — Filter + semantic ("populist speeches about corruption")

Restrict the candidate pool to populist speeches, then rank by topical similarity:

```python
search_speeches(
    q="corupție justiție DNA",
    rank_fusion="rrf",
    filter={"enrichments.discourse.hawkins.score": 2}
)
```

ES DSL:
```json
{
  "knn": {
    "field": "enrichments.embedding",
    "query_vector": [...],
    "filter": {"term": {"enrichments.discourse.hawkins.score": 2}},
    "k": 20,
    "num_candidates": 200
  }
}
```

The `filter` clause inside `knn` runs *during* the candidate scan — only H=2 speeches enter the kNN ranking. Faster than post-filter; no quality loss because we're filtering on a high-cardinality keyword.

This is the operator's bread-and-butter pattern. "Populist anti-corruption rhetoric" / "anti-pluralist speeches about media" / "low-DQI speeches about the constitution" all fit this shape.

### Pattern 3 — Anchor-driven discovery ("speeches like this one")

Start from a seed speech the journalist found compelling. Find semantically-similar speeches and re-rank by discourse score:

```python
seed = get_speech("mo://2025/II/119#agenda-1#act-134")
seed_vec = seed["enrichments"]["embedding"]

# Find topically-similar speeches, then sort by anti-pluralism strength
search_speeches_knn(
    query_vector=seed_vec,
    page_size=20,
    sort=[
        {"enrichments.discourse.vparty.score": "desc"},
        "_score"  # break ties by cosine similarity
    ]
)
```

The embedding finds the candidate pool ("speeches in this rhetorical neighbourhood"); the discourse score promotes the strongest examples.

This is the pattern behind "more like this" buttons in the journalist UI — but with editorial weight (V=2 first, then V=1, then by similarity).

### Pattern 4 — Cross-tab aggregation (the four-cell H × V grid)

The headline analysis chart from `docs/discourse-pilot-baseline-2026-05.md` § 11. Pure aggregation, no semantic component:

```python
es.search(index="mo-speeches", body={
    "size": 0,
    "query": {"exists": {"field": "enrichments.discourse"}},
    "aggs": {
        "hawkins": {
            "terms": {"field": "enrichments.discourse.hawkins.score", "size": 5},
            "aggs": {
                "vparty": {
                    "terms": {"field": "enrichments.discourse.vparty.score", "size": 5}
                }
            }
        }
    }
})
```

Returns the 9-cell grid (H ∈ {0,1,2} × V ∈ {0,1,2}). The illiberal-populism cell (H=2 + V≥1) is the load-bearing journalistic finding.

Per Q5 of the design doc: aggregations on `byte` fields are sub-millisecond at corpus scale — the whole 9-cell grid returns in <50 ms even on the 19,200-speech backfill.

### Pattern 5 — Negative filtering ("anti-corruption speeches NOT in populist register")

Useful for journalism: separate principled rhetoric from opportunistic instrumentalisation:

```python
search_speeches(
    q="combaterea corupției",
    rank_fusion="rrf",
    filter={
        "enrichments.discourse.hawkins.score": 0,
        "enrichments.discourse.dqi.level_of_justification": {"gte": 2}
    }
)
```

This finds speeches semantically about anti-corruption (BM25 + kNN) that are simultaneously NOT populist (H=0) AND high-quality reasoning (DQI ≥ 2). Lets a journalist surface "good faith" anti-corruption rhetoric distinct from the populist register.

The filter shape is critical for editorial credibility: without the `H=0` clause, the search would surface populist anti-corruption rants alongside genuine policy debate.

### Pattern 6 — Time-series cross-tab (politician × score × year)

Person-focused journalism: how does a speaker's H-score evolve?

```python
es.search(index="mo-speeches", body={
    "size": 0,
    "query": {"term": {"speaker.person_id": "georgescu-calin"}},
    "aggs": {
        "by_year": {
            "date_histogram": {"field": "session_date", "calendar_interval": "year"},
            "aggs": {
                "hawkins": {"terms": {"field": "enrichments.discourse.hawkins.score"}},
                "avg_h": {"avg": {"field": "enrichments.discourse.hawkins.score"}}
            }
        }
    }
})
```

The denormalised `speaker.person_id` makes this a single ES query. Drives `/politicieni/<slug>` pages: "Călin Georgescu's populism index over 2020–2025".

### Pattern 7 — Stylistic clustering (speakers with similar discourse profiles)

Different from topical similarity. Two speakers with `(H=2, V=2, DQI-low)` profiles are *rhetorically* similar, even when speaking about different topics. Captured purely by aggregation on the discourse profile vector:

```python
# Find speakers whose average H + V is high
es.search(index="mo-speeches", body={
    "size": 0,
    "query": {"exists": {"field": "enrichments.discourse"}},
    "aggs": {
        "by_speaker": {
            "terms": {"field": "speaker.person_id", "size": 50},
            "aggs": {
                "avg_h": {"avg": {"field": "enrichments.discourse.hawkins.score"}},
                "avg_v": {"avg": {"field": "enrichments.discourse.vparty.score"}},
                "speech_count": {"value_count": {"field": "record_id"}},
                "high_v_count": {
                    "filter": {"range": {"enrichments.discourse.vparty.score": {"gte": 1}}}
                }
            }
        }
    }
})
```

Sort speakers client-side by `(avg_h + avg_v) / 2` for the rhetorical-extreme leaderboard. The combination is more useful than either score alone — high H without V is "rhetorical populism" (Sanders / Tsipras flavour); high V without H is "technocratic anti-pluralism" (rare in our corpus); high H + V is the illiberal cluster.

## What discourse fields don't do

A short list of misconceptions worth flagging:

- **They don't power kNN.** A speech with `H=2` is not "closer" in vector space to another `H=2` speech. The vector is computed from text content, not discourse score. If you want rhetorical-style similarity (Pattern 7), aggregate on the discourse fields directly.

- **They don't replace text matching.** Filtering on `H=2` doesn't tell you *what* the speech is populist about — you still need BM25 / kNN to surface the topic. Discourse fields restrict the universe; semantic search ranks within it.

- **They aren't a knowledge base.** The `markers[]` evidence is auditable — and as of v0.16.x it's also indexed in ES under `enrichments.discourse.{framework}.markers[].evidence.{text, char_range}` — but those snippets aren't searchable as facts. They're rationale anchors for the holistic score, not retrievable assertions: a populist speech that quotes a CCR ruling will have `evidence.text` containing words from that ruling, but the marker's role is to anchor *why the speech is populist*, not to commit to the ruling's truth content.

- **They aren't faithful to any single political-science school.** Hawkins is one of several populism rubrics; V-Party is one anti-pluralism operationalisation. Other frameworks (Mudde "thin ideology", Müller "moralised antipluralism", Schedler "authoritarian incumbency") would yield different scores on the same speeches. Stage 5 of the build order adds custom rubrics; v0.1 ships these four because they were calibration-validated.

- **They don't enforce confidence thresholds at query time.** Both Hawkins and V-Party emit `framework_confidence` (0–1). Production queries should consider filtering on `framework_confidence >= 0.7` for editorial-grade results, but the producer doesn't auto-discard low-confidence calls. The Opus-escalation hybrid (Stage 5 of the build order) addresses this: route low-confidence Flash-Lite calls to Opus for a second opinion.

## UX patterns in the journalist UI / agent layer

The retrieval primitives compose into a small set of UI surfaces. Each is one line of `lib/search.ts` (Next.js) backed by the typed query layer in `src/monitorul_ii/elasticsearch/queries.py`.

### Faceted search page

```
┌─────────────────────────────────────────────────────────┐
│ Search: [reforma justiției] _________________________    │
├─────────────────────────────────────────────────────────┤
│ Filters:                                                 │
│   ◉ All  ○ Populist (H=2)  ○ Anti-pluralist (V≥1)        │
│   Year: [2020 ━━━━━━━━ 2026]                             │
│   Chamber: ☑ Camera Deputaților  ☑ Senatul              │
│   Min DQI: [≥0 ▼]                                       │
├─────────────────────────────────────────────────────────┤
│ Results (1,247 speeches):                                │
│   • Diana Șoșoacă, 2025-04-15  [H=2 V=2 DQI=0]          │
│     "Această dictatură juridică..."                      │
│   • Călin Georgescu, 2024-11-30  [H=2 V=1 DQI=1]        │
│     "Statul paralel a confiscat..."                      │
└─────────────────────────────────────────────────────────┘
```

Backed by `search_speeches(q=..., rank_fusion="rrf", filter={...})`. Each filter chip toggles a `term` clause; the badge `[H=2 V=2 DQI=0]` is read straight from the per-hit `enrichments.discourse.*`.

### Politician profile page (`/politicieni/<slug>`)

```
┌─────────────────────────────────────────────────────────┐
│ Diana Șoșoacă                     S.O.S. România        │
├─────────────────────────────────────────────────────────┤
│ Discourse profile:                                       │
│   Populism (H):     ▆▆▆▆ 1.7 / 2 (corpus median: 0.3)   │
│   Anti-pluralism:   ▆▆▆▆ 1.8 / 2                        │
│   DQI quality:      ▂      0.4 / 3 (corpus median: 1.5) │
│   Voice attribution: 92% first-person                    │
├─────────────────────────────────────────────────────────┤
│ H-trajectory 2020–2025:  (sparkline of yearly avg H)    │
│ Most-cited speeches:                                     │
│   • [H=2 V=2] "Această dictatură juridică..." 2025-04-15│
│   • [H=2 V=2] "Vom da în judecată..."        2024-11-30 │
└─────────────────────────────────────────────────────────┘
```

Backed by `person_page(person_slug)` — Pattern 6 with one `term` filter on `speaker.person_id` plus aggregations on each discourse field.

### Smart digest ("populist speeches this week")

A daily-cron script that runs:

```python
search_speeches(
    rank_fusion="bm25-only",  # no semantic ranking needed
    filter={
        "enrichments.discourse.hawkins.score": 2,
        "session_date": {"gte": "2026-01-01"}  # past week
    },
    sort=[{"enrichments.discourse.framework_confidence": "desc"}]
)
```

Top-10 speeches, filtered to high-confidence H=2, sorted by confidence. Posts to a Slack channel or generates a newsletter. **No LLM call at query time** — the LLM ran at coding time, the digest is a one-line filter+sort.

### LLM-agent tool wrappers

The agent layer (per Q9) exposes the typed query functions as tools. The agent picks the right primitive based on user intent:

- "Show me populist speeches about migrants" → `search_speeches(q="imigrație migranți", filter={"hawkins.score": 2})`
- "How does AUR compare to PSD on anti-pluralism?" → two `agg_speeches_by_party_year` calls, side-by-side
- "Find speeches similar to this one but more measured" → Pattern 3 with `filter={"hawkins.score": {"lte": 1}}`

The discourse fields surface as "facets the agent can reason about", much like the chamber/year/party facets. The agent doesn't need to understand the rubrics deeply — it just knows that `hawkins.score=2` means "more populist" and routes accordingly.

### Rendering markers + rationale + evidence highlighting (per-speech page)

The per-speech page (`/discurs/<slug>`) renders the speech body with each evidence anchor highlighted and a side panel listing markers with their rationale. Everything the renderer needs is in one ES doc — no extra fetch:

```ts
// Pseudocode for the React component
const speech = await getSpeech(record_id);  // single ES request
const { text, enrichments } = speech._source;
const { hawkins, vparty, dqi, voice } = enrichments.discourse;

// 1. Render highlighted evidence inside the speech body
const spans = collectAllMarkers([hawkins, vparty, dqi]).map(m => ({
  range: m.evidence.char_range,           // [start, end) into `text`
  framework: m.framework,
  kind: m.kind,
  rationale: m.rationale_short,
  voice: voice?.classifications?.find(c => c.marker_id === m.id)?.voice,
}));
// React: map spans to <mark data-framework="hawkins" data-voice="speaker_first_person">…</mark>
// where the highlighted slice is text.slice(range[0], range[1]).

// 2. Side panel — one card per marker
markers.map(m => (
  <MarkerCard
    framework={m.framework}                // "Populism (Hawkins)" / "Anti-pluralism (V-Party)" / "Deliberative quality (DQI)"
    kind={m.kind}                          // chip label
    confidence={m.marker_confidence}       // sparkline
    rationale={m.rationale_short}          // body text
    evidenceText={m.evidence.text}         // verbatim quote
    voice={voiceForMarker(m.id)}           // colour
  />
));

// 3. Footer — framework-level rationale
<RationalePanel
  hawkins={hawkins.rationale}              // "Discursul prezintă elemente populiste accentuate."
  vparty={vparty.rationale}
  dqi={dqi.rationale}
  hawkinsVersion={hawkins.framework_version}  // "hawkins@2018" — hover tooltip
/>
```

A few load-bearing properties:

- **`char_range` survives Romanian typography drift.** The producer's `find_text_offsets_tolerant` is called at INDEX TIME by `denormalize.py`, so even when the model paraphrases curly quotes / dashes / ellipsis the offsets land byte-correct in `text`. The browser does NOT need to re-implement the typography fold.
- **Char-range absent ⇒ paraphrase, not a bug.** When the model genuinely reworded the evidence (added/removed words), `char_range` is omitted but `evidence.text` still ships. The renderer can fall back to `text.indexOf(evidence.text)` and either highlight the imperfect match or show the marker in the side panel only.
- **`voice.classifications[].marker_id`** matches the index of the corresponding marker in the same framework's `markers[]` array (`m_0`, `m_1`, …). Use it to colour each highlighted span by attributed voice (speaker's own voice / quoted opposition / hypothetical / negated / etc.). Voice attribution is what tells the journalist whether a populist marker is the speaker's *own claim* or them *quoting an opponent to refute it*.
- **Markers are an `object` field, not `nested`.** The full array round-trips via `_source` regardless. Per-marker filtering at query time (e.g. "Hawkins markers whose rationale mentions democracy") would need the `nested` type, which we'd graduate to if the query patterns demand it.

Mapping update path for any marker-shape change: `monitorul-ii es-init --update-mappings` (additive only, safe + idempotent), then `monitorul-ii index pdfs/ --force` to backfill every existing speech. No new generation index needed unless a field's TYPE changes.

## Future extensions

### A. Discourse-as-vector for stylistic similarity

Currently a speech's `(H, V, DQI-level, voice-distribution)` profile is a 4-dimensional categorical tuple. We could embed it as a small dense vector and add a third RRF leg:

```
RRF(q) = fuse([
    BM25(text, q),                      # word match
    kNN(enrichments.embedding, q_vec),  # topical similarity
    kNN(enrichments.discourse_vec, target_profile_vec),  # rhetorical similarity
])
```

The third leg would let the operator say "find speeches like this *rhetorically* (populist + anti-pluralist + low-DQI), regardless of topic". Out of scope for v0.1 — the four scalar fields cover the operator's actual needs, and a dedicated stylistic-similarity vector is solving a hypothetical problem.

### B. Confidence-aware retrieval

Add a default filter `framework_confidence >= 0.7` to all production queries; surface lower-confidence speeches under an "experimental" toggle. Editorial-grade by default, exploratory mode opt-in.

### C. Score-weighted re-ranking

Post-retrieval, multiply the cosine score by a discourse-weighted boost:

```
final_score = cosine_score × (1 + α × hawkins.score × hawkins.framework_confidence)
```

Where α ∈ [0, 1] is the operator-tuned populism boost. At α=0 you get pure semantic search; at α=1 you get a populism-skewed ranking. Useful for experimental "populism-flavoured search" UIs without committing to a hard filter.

### D. Cross-rubric ensemble queries

Once Stage 5+ adds CMP / CHES / securitization rubrics, queries can compose across them:

```python
filter={
    "enrichments.discourse.hawkins.score": 2,           # holistic populism
    "enrichments.discourse.cmp.left_right": {"lte": -2}, # left-leaning
    "enrichments.discourse.securitization.score": 1,     # mild securitisation
}
```

Three rubrics fired by three different prompts at coding time; one filter clause at query time.

## Implementation pointers

The query layer already exposes filter parameters:

- `search_speeches(filter={...})` — accepts a dict of `{field: value | range}`. Field names must match the ES mapping path (`enrichments.discourse.hawkins.score`).
- `agg_speeches_by_party_year` — supports nested aggregations on any keyword/numeric field.
- `list_document_children` — purely a per-document playback query; doesn't filter on discourse (every record in a doc is shown regardless).

To add new query patterns, edit `src/monitorul_ii/elasticsearch/queries.py` (the canonical query layer) and register the new function in `NAMED_QUERIES` (the dispatch table the `monitorul-ii query` CLI consumes). The Python sister to the future Next.js `lib/search.ts` per Q9.

The mapping in `src/monitorul_ii/elasticsearch/mappings/mo-speeches.json` declares every queryable discourse field. Mapping changes propagate via `monitorul-ii es-init --update-mappings` (additive only — adding fields is safe and idempotent; type changes need a new generation).

## Cost / latency at the search layer

Crucially: **all of these query patterns are sub-100ms at corpus scale**. The LLM ran at coding time, not at query time. The discourse fields are pre-computed scalars in ES; filtering / aggregating on them is cheap.

| Query | Typical latency | Notes |
|---|---|---|
| Filter + BM25 | < 30 ms | Linear in matched docs |
| Filter + kNN (HNSW) | < 50 ms | Logarithmic in corpus size |
| Filter + RRF | < 80 ms | Two ES round-trips |
| 4-cell cross-tab agg | < 50 ms | Pure cardinality on `byte` fields |
| Person-page (multi-agg) | < 100 ms | One query; multiple parallel aggs |

This is the leverage of the offline-coding model: the cost moves from the user-facing query path to the producer's coding path. Once a speech is coded, querying its discourse profile is free.

## See also

- `prompts/{hawkins_populism_v1, voice_classifier_v1, dqi_v1, vparty_antipluralism_v2}.{md, schema.json}` — the rubrics themselves.
- `src/monitorul_ii/extraction/enrichments/discourse.py` — the producer.
- `src/monitorul_ii/elasticsearch/queries.py` — the canonical query layer.
- `src/monitorul_ii/elasticsearch/denormalize.py` — the discourse → ES projection (`_flatten_discourse_payload`, `_flatten_marker_framework`, `_flatten_voice`, `_flatten_dqi`).
- `src/monitorul_ii/elasticsearch/mappings/mo-speeches.json` — the discourse field mappings.
- `docs/discourse-analysis-schema.md` — design decisions Q3–Q11.
- `docs/discourse-pilot-baseline-2026-05.md` — calibration sweeps and the four-cell H × V design.
