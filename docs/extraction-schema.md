# Extraction schema — structured JSON for Monitorul Oficial Partea II

Companion to [`architecture.md`](./architecture.md). That file documents the fetch + convert pipelines (PDF → markdown). This file documents the next layer: **markdown → structured JSON**, suitable for full-text search (Elasticsearch later) and for retrieval-augmented generation (RAG over LLMs).

This doc is the design record. It walks through every shape decision with the rationale that produced it, so future contributors can understand *why* the schema is what it is, not just *what* it is. The consolidated schema appears at the end.

## Mission

Romania's Official Gazette, Part II is the legal record of parliamentary plenary sessions and committee work. Its democratic-transparency role spans three audiences:

- **Lawyers and judges** — interpret ambiguous laws by reading the *intent of the legislator* (the debate that produced the text). Need exact-source fidelity, citable spans, and stable references to bills and decisions.
- **Journalists and citizens** — hold politicians accountable for spoken arguments and votes. Need cross-document politician identity, party-affiliation timelines, and topic-scoped queries ("all education debates from this legislature").
- **Historians and political scientists** — chronicle the evolution of Romanian political discourse. Need a complete, queryable corpus over the lifetime of the publication.

The schema must serve all three. That dictates priorities: **deterministic faithfulness over cleverness**, **per-occurrence truth over derived "current" state**, **explicit unknowns over silent omission**.

## Design tree

The schema was designed by walking the decision tree top-down. Each subsection records a decision and the alternatives that were rejected, so the schema's shape is auditable.

### 1. Granularity — hierarchical canonical + flat projections (later)

**Decision.** One canonical JSON per markdown file (1:1 with PDFs). Hierarchical: a document contains agenda items, agenda items contain activities, activities contain references. **Flat row-oriented projections** (one row per speech, one row per vote, etc.) are *not* materialized at this stage — they will be derived later as Elasticsearch ingest indices.

**Why hierarchical-canonical.** The natural unit of meaning in a plenary session is *the debate on a bill*. The chair opens an agenda item, the raporteur presents the committee report, group representatives speak in turn, the chamber votes (or defers). Reconstructing that flow from a flat speech list forces every consumer to re-stitch parent-child relationships. Storing the tree directly preserves the question shape ("what happened with PL-x 257/2019?") without auxiliary joins.

**Why not one-record-per-speech as canonical.** A flat-only schema must denormalize agenda context onto every row (`agenda_item_title`, `bill_ref`, `chamber`, `session_date`...) and the redundancy will drift the moment any field needs correction. The hierarchical canonical *can* generate the flat view; the flat view cannot reconstruct the tree.

**Why flat projections are deferred.** Elasticsearch will produce flat indices natively at ingest time. Maintaining a separate `speeches.jsonl` step before ES is wired up is a load-bearing assumption we don't have to make yet. The hierarchical JSON is the source of truth; ES picks it up later.

### 2. Document type taxonomy — discriminated union with three starter types

**Decision.** Top-level `document_type: "plenary_stenogram" | "committee_synthesis" | "other"`. Shared envelope (frontmatter, ids, paths, extraction provenance) wraps a typed `body`.

**Observed types.** Sampling across 2000–2026 surfaced two clear genres:

- **`plenary_stenogram`** — full transcripts of plenary sessions (Senate or Camera Deputaților). Speaker turns formatted as `## **Domnul/Doamna [Name]:**`, vote events with `Cine este pentru? / Împotrivă? / Abțineri?` protocol, agenda items with bill-code references. Issue numbers are bare integers (`58/2013`, `48/2026`).
- **`committee_synthesis`** — summaries of committee meetings over a period (e.g., "Perioada 8–11.04.2013"). Per-committee blocks with attendance lists, brief agenda items, and **narrative** vote outcomes ("aprobat cu majoritate de voturi (6 voturi împotrivă și două abțineri)"). No individual speaker turns. Issue numbers carry a `c` suffix (`13c/2013`, `1c/2000`).

**Why a discriminated union and not "one universal schema with optional fields".** A plenary speech and a committee meeting are categorically different objects with different participants, granularity, and queryable axes. A universal schema with everything optional means consumers see nulls everywhere and cannot tell whether the source has the structure or whether the extractor missed it. The discriminator makes that explicit: a `committee_synthesis` document does not have `speeches` because the source genre does not record them.

**Why three types, not two.** `other` is the explicit catch-all for layouts we have not yet profiled — joint sessions of both chambers, oath-taking ceremonies, special protocols, future formats. The contract is: **every document classifies into something**. `other` carries minimal structure (frontmatter + headings + raw excerpt + audit reason) so the document is still findable in search and the pipeline never silently fails on an unknown layout. Misclassifying a joint session as `plenary_stenogram` would produce garbage agenda items; punting to `other` is the honest move.

**Detection rule (starter).** Issue suffix `c` → `committee_synthesis`. Else, body contains `(STENOGRAMA)` or `DEZBATERI PARLAMENTARE` → `plenary_stenogram`. Else `other`. Cheap to implement, easy to inspect, easy to extend.

### 3. Extraction approach — hybrid with per-section provenance

**Decision.** Hybrid: regex / parser for the cheap deterministic fields, LLM for the rest. Every extracted record (agenda item, activity, reference, interpellation) carries an `extraction` block recording who produced it, when, with what confidence, and from which span of the source markdown.

**Why hybrid, not pure-regex.** Regex can extract speaker turn boundaries (`## **Domnul/Doamna X:**` is unambiguous), agenda enumeration (`\d+\.` numbering), reference codes (`PL-x`, `Pl-x`, `L###`, `COM(YYYY) NNN`), vote tallies (`Cine este pentru? / Împotrivă? / Abțineri?` with optional integer counts), party affiliations (`Grupul parlamentar al X`), and the YAML frontmatter the converter already produces. That covers ~70% of useful structure deterministically and replayably. But regex cannot resolve "Domnul Crușoveanu" / "Marian Crușoveanu" / "domnul deputat Crușoveanu" to a single politician across decades, cannot classify a debate as "education" vs "transport", cannot infer support/opposition stance. Those need an LLM layer.

**Why hybrid, not pure-LLM.** LLM extraction is non-deterministic on fields where determinism matters (vote counts, reference codes, dates), expensive to rerun across thousands of documents on every schema bump, and silently goes wrong on weird layouts (the `other` bucket). Regex is free, deterministic, and predictably fails on layouts it does not match — which is exactly what you want for a pipeline that will run for years.

**Why per-section provenance, not per-field.** Per-field provenance would inflate the JSON 3-4x with metadata. Per-section (one `extraction` block per agenda item, per activity, per reference) is the sweet spot: granular enough to know which extractor produced any given chunk, cheap enough to not balloon the on-disk size.

**Source spans are mandatory.** Every extracted record includes a `source_span: { lines, chars, content_sha }` pointing back to the canonical markdown. This enables three things future-you will want:

- **Citation rendering.** A RAG pipeline can answer "from line 432 of `2026-04-29_MO-PII-48-2026.md`" with no extra bookkeeping.
- **Human verification.** Lawyers/journalists clicking through to the original passage trust the structured record more.
- **Drift detection.** When the markdown gets re-converted (better PDF→MD pipeline, fixed cleanup bug), the per-span `content_sha` flags every record whose source bytes changed — invalidating downstream embeddings/cache without manual scanning.

**Field-level convention.** Schema-level convention: regex-derived fields are required (or explicitly `null`), LLM-derived fields are optional and clearly marked. This makes it visually obvious in any record which parts are deterministic and which are model-generated — important for legal/journalism use cases that may want to cite the deterministic and flag the LLM-inferred.

### 4. Plenary body — nested under agenda + sibling interpellations + session envelope

**Decision.** The body of a `plenary_stenogram` is:

- `session: {...}` — chair, secretaries, attendance, quorum, opened_at, format
- `agenda_items: [{ ordinal, title, primary_reference, activities: [...] }]`
- `interpellations: [{...}]`

Activities are an ordered, discriminated list of `speech | vote | procedural | narrator | deferral`.

**Why nested under agenda, not flat sibling arrays.** The unit a journalist or lawyer reaches for is the debate on a single bill: chair opens it, raporteur presents, debates open, group reps speak, vote (or deferral), chair closes. That's one agenda item. Nesting matches the question. Flat sibling arrays (`speeches[]`, `votes[]`, `agenda_items[]` joined by ID) force every consumer to re-stitch — fine for an RDBMS, hostile for a canonical document.

**Why nested, not timeline-of-events.** A pure timeline (`events[]` with `agenda_open | speech | vote | agenda_close | ...`) preserves chronology but makes natural queries miserable. Answering "what was the vote on bill X" requires walking the event stream and reconstructing state. Closer to a debug log than a structured document.

**Why interpellations are a sibling block, not nested in agenda.** They procedurally are a peer block — the chair literally announces "trecem la primirea răspunsurilor la interpelări" after closing the agenda. They have their own grammar (questioner → ministry → topic → question → response, often deferred to writing) that does not share fields with regular speeches. Forcing them under the agenda would either invent a fake parent item or pollute the agenda's activity types.

**Why `session` as an envelope.** Chair, attendance, quorum apply to the whole session, not to any one agenda item. They belong outside the agenda array. `format: "in_person | online | mixed"` matters since 2020; `special_procedure` slot is reserved for budget debates, no-confidence votes, government question time — sub-genres of plenary that can be flagged without spawning new top-level types.

**Activity type taxonomy (5 starters).**

- `speech` — standard speaker intervention. Carries `speaker`, `text`, `references_mentioned[]`.
- `vote` — formal vote with counts (or deferral). Discussed in detail in section 8.
- `procedural` — chair-driven motions: agenda approval, programme of work, urgency procedure. Distinguished from `speech` because the chair speaking is procedural noise, not debate.
- `narrator` — italicized stage directions: `_(Aplauze.)_`, `_(Se păstrează un moment de reculegere.)_`, `_(I se întrerupe microfonul.)_`. Kept as their own type so UIs/LLMs can filter or surface them rather than mixing into speech text.
- `deferral` — the formulaic "Aceasta rămâne pentru votul final." Modeled distinctly from `vote` because no vote happened; the item was queued for batch voting later. `defers_to` is the back-link slot when the eventual final-vote document is identified.

Resist further fragmentation. If a new pattern emerges (e.g., named-vote roll calls), evaluate whether it fits inside `vote` with a `voting_method` discriminator before adding a sixth activity type.

### 5. Speaker identity — backfillable person_id + per-occurrence party and role

**Decision.** Every place a person appears uses a uniform `Speaker` shape:

```json
{
  "raw": "Domnul Marian Crușoveanu",   // verbatim source string — audit trail
  "name": "Marian Crușoveanu",         // parsed-out name
  "title": "deputat",                  // deputat | senator | secretar de stat | ministru | etc
  "role": "raportor",                  // role at this turn — see below
  "party_group": "PNL",                // attributed at this moment in this document
  "person_id": null                    // backfilled when the person registry exists
}
```

**Why a uniform shape across every callsite.** Chairs, secretaries, raporteurs, group representatives, interpellators, vote proposers, committee attendees — all are people. One shape, used uniformly. When `person_id` is backfilled, every callsite resolves at once.

**Why `person_id` is nullable from day one and backfilled later.** Identity resolution is a real sub-project: married-name changes, diacritic variants ("Ștefan" / "Stefan"), common surnames ("Popescu", "Ionescu") with multiple bearers across mandates, transliteration drift, deputy substitutions during a single meeting. Don't block the corpus on it. Ship structured records with `person_id: null`; backfill when the registry comes online. Backfill is a single column update across stored JSON; no re-parse of source markdown.

**Why party affiliation is per-occurrence, not per-person.** Politicians switch parties (PSD → PRO România → independent → splinter). The schema records the party-group as *attributed at this moment in this document* — typically what the chair literally announces ("Grupul parlamentar SOS România", "Grupul parlamentar al USR"). A speech in 2018 marked PSD stays PSD on that record forever, even if the politician moved later. This is more truthful to the source AND naturally yields a party-affiliation timeline when the registry is built — without the registry having to lie about a single "current party" field.

**Why role is per-occurrence too.** The same MP can be raporteur in agenda item 1, group representative in item 2, chair in item 3, interpellator in item 9. Capture the role as observed at each turn. The eventual registry carries person identity only; roles live on the speech.

**Implication for the eventual registry.** `persons.jsonl` (or whatever data store) carries minimal *stable* facts: canonical name, alternate-name forms, birth year (for disambiguation), known mandate windows. It does *not* carry "current party" or "current role" — those are per-occurrence. Less drift, less correctness debt, no rewrite needed when the truth shifts.

### 6. Reference normalization — single discriminated array, primary-vs-mentioned split

**Decision.** Two related arrays at different levels:

- `agenda_items[].primary_reference: Reference | null` — the *one bill the agenda item is about*. First-class.
- `activities[].references_mentioned[]: Reference[]` — refs cited in passing during a speech, vote motion, or interpellation.

Both use the same discriminated `Reference` union with `type: bill | law | oug | og | court_decision | constitution | regulation | eu_doc | treaty`.

**Why a single discriminated union, not 8 sibling arrays.** "Find all sessions that mention Law X" wants every reference type in one searchable rollup. Two arrays force every consumer to query both. Eight arrays compound the problem. The discriminator preserves type-specific fields (court decisions have a date + MO publication, EU docs have a COM number + subseries, bills have a chamber-of-origin prefix) without lowest-common-denominator flattening. One array per location is also easier to render in UIs.

**Why the primary-vs-mentioned split.** An agenda item is *about* one bill (its `primary_reference`) but the discussion may *cite* dozens of laws/OUGs/court decisions in passing. These are categorically different roles. The query "show me the debate on PL-x 257/2019" matches the agenda item where it's primary — not every passing mention. Without the split, search returns mostly noise.

It also matches Romanian parliamentary procedure: the chair literally announces the bill code when opening the item ("Punctul 4 din ordinea de zi – Proiectul de lege pentru aprobarea OUG nr. 23/2013 ... PL-x 95/2013"). The bill code in the chair's opening *is* the agenda item's identity. Other refs are atmospherics around the debate.

**Type-specific fields per discriminator (starter set).**

| `type` | Fields | Notes |
|---|---|---|
| `bill` | `prefix` ("Pl-x" / "PL-x" / "L"), `number`, `year`, `secondary_year` (optional, for resubmissions like `132/2022/2023`), `chamber_of_origin` (derivable: `Pl-x`/`PL-x` → camera, `L###` → senat), `category` ("ordinară" / "organică" / "constituțională") when stated | `secondary_year` captures resubmissions where a bill was returned for re-examination |
| `law` | `number`, `year`, `subject` (best-effort short label from context) | `subject` filled when the citing sentence names what the law is about |
| `oug` / `og` | `number`, `year`, `issuer` ("Guvern" / "Președintele") | OUG = ordonanță de urgență, OG = ordonanță (non-urgent) |
| `court_decision` | `court` ("Curtea Constituțională" / "ICCJ"), `number`, `date`, `published_in` (often "MO Partea I, nr. X din Y") | Reexamined laws cite the CCR decision that triggered the reexam |
| `constitution` | `article`, `paragraph` | E.g. "art. 73 alin. (1)" |
| `regulation` | `body` ("Camera Deputaților" / "Senatul"), `article`, `paragraph` | Internal procedural rules cited during debate |
| `eu_doc` | `series` (COM/JOCE/REG/DIR), `year`, `number`, `subseries` (final/F2/etc), `subject` (optional) | Europe-related debates cite a lot of these |
| `treaty` | `name`, `signed_date` (if given) | Catch-all for less common cites |

**Every reference carries `raw` and `char_offsets`.** `raw` is the verbatim source string (audit trail). `char_offsets` are the location within the containing activity's text — your provenance trail per section 3, but at sub-section granularity for refs.

**Backfill path.** The eventual analog of the person registry — a `laws_registry`, `bills_registry`, `court_decisions_registry` — gives each reference a stable `*_id`. Same nullable pattern as `person_id`. Reference codes are the canonical join key.

### 7. Identifiers — hierarchical paths + content-hash

**Decision.** Document IDs are hierarchical URI-style paths: `mo://YYYY/PART/ISSUE`. Sub-records compose with fragment paths: `mo://2026/PII/48#agenda/4/activity/7`. Every record additionally carries a `content_sha` (truncated SHA-256) as a content fingerprint.

**Why hierarchical paths as canonical IDs.**

- Compose down naturally: document → agenda item → activity. No external ID generator needed.
- Human-readable in logs, URLs, citations, error messages. Lawyers and journalists can paste the ID into a citation and the reader reconstructs what it points to.
- Regenerable: re-running the extractor on the same document produces the same IDs. No UUID stability bookkeeping.
- Sortable lexically and chronologically (`2026/PII/48 > 2026/PII/47`), which makes pagination and "next/previous" trivial.

**Why content-hash is the secondary fingerprint.** The path identifies *position*; the hash identifies *content*. Both matter:

- Drift detection. Re-extract `mo://2026/PII/48#agenda/4` and the hash changed → the agenda item's parsed content has shifted → invalidate downstream embeddings/cache.
- Refactor regression checks. Running an extractor refactor and getting *zero* hash changes is a cheap end-to-end safety net.

**Why not UUIDs.** Opaque. Lose the "I can read this and know what it points to" property. And UUIDs aren't stable across re-extraction without a separate identity-resolution layer mapping last-week's UUID to this-week's record.

**Why not composite-tuple keys (`(year, part, issue, agenda_ordinal, activity_ordinal)`).** That *is* a composite key — hierarchical paths are composite keys with a delimiter. The path form serializes cleanly into URLs and citations; the tuple form forces every consumer to carry the full tuple. Same data, friendlier presentation.

**Composition with the existing fetch infrastructure.** The document-level ID `mo://YYYY/PART/ISSUE` matches the natural key already used in the SQLite `issues` table from the fetch pipeline. No parallel ID universe.

### 8. Source spans + the vote/interpellation/topic shapes

**Source spans.** Every extracted record carries:

```json
{
  "lines": [432, 458],            // human-readable, easy to navigate in editors
  "chars": [12480, 13205],        // precise, robust to line wrapping
  "content_sha": "a3f9c1d2e4b8"   // detects drift if source markdown is re-converted
}
```

All three are cheap (~50 bytes per record). Each alone is insufficient: line ranges are display-friendly but ambiguous around wrapping; char offsets are precise but unreadable; hashes survive reformatting but not edits. Together they cover every legitimate need.

**Vote shape — the deferred-vote nuance is critical.** Modern Romanian parliamentary procedure batches *most* voting to a "votul final" block at the end of the session. During the debate, the chair almost always says "Aceasta rămâne pentru votul final" with no numeric counts. That is *not* a missing vote; it is a deferred vote.

```json
{
  "type": "vote",
  "motion_text": "...",                        // verbatim chair announcement
  "motion_type": "procedural | amendment | final | item_adoption | agenda_approval | urgency_procedure | report_approval",
  "voting_method": "electronic | show_of_hands | secret_ballot | nominal | by_acclamation",
  "timing": "live | deferred",                 // critical — most modern votes are deferred
  "counts": {
    "for": null | int | "unanimous",           // "Mulțumesc." with no number = unanimous
    "against": null | int,
    "abstain": null | int,
    "total_voting": null | int
  },
  "outcome": "approved | rejected | tied | deferred | no_quorum",
  "quorum_announced": null | int,
  "proposed_by": Speaker | null,
  "nominal_breakdown": null | [{ "person_id": "...", "vote": "for|against|abstain|absent" }],
  "source_span": SourceSpan,
  "extraction": PerSectionExtraction
}
```

**Two non-obvious modeling choices.**

- `counts.for` accepts `null` (not stated), `int`, or the literal string `"unanimous"`. The Romanian convention is for the chair to say "Mulțumesc." (= unanimous, no count stated) instead of an integer when the vote is unanimous. Coercing this to a number is editorializing; preserving the literal protocol is honest. Search/RAG consumers can normalize at their layer.
- `defers_to` (slot on `deferral` activities) and a corresponding `resolves: { document_id, ... }` (slot on `vote` activities with `motion_type: "final"`) are the eventual cross-link between the deferral and its resolving final-vote document. Initially nullable; backfilled by a cross-document linker pass.

**Interpellation shape — `response_deferred` is the equivalent nuance.**

```json
{
  "questioner": Speaker,
  "addressed_to": "Ministerul Educației și Cercetării",
  "addressed_to_normalized": null,             // future ministry registry
  "interpellation_number": "1.172B" | null,    // formal Camera tracking number when stated
  "topic": "...",                              // chair's announced title
  "question_text": "..." | null,               // often null — questions are filed in writing
  "response": { "speaker": Speaker, "text": "..." } | null,
  "response_deferred": true | false,           // ministry may answer in writing later
  "source_span": SourceSpan,
  "extraction": PerSectionExtraction
}
```

`addressed_to_normalized` mirrors the `person_id` nullable-pattern. Ministries rename and merge frequently in Romania ("Ministerul Comunicațiilor" → "Ministerul Cercetării, Inovării și Digitalizării" → "Ministerul Economiei, Digitalizării..."). Backfilled when a ministry registry exists.

**Topics — closed primary + open secondary.** Topic tagging is the cross-cutting feature that powers "show me all education debates from this legislature" and similar accountability queries.

```json
"topics": {
  "primary": ["Transporturi"],                 // closed taxonomy, ~15 categories
  "secondary": ["drumuri-naționale", "publicitate-stradală"]  // open free tags from LLM
}
```

- **Closed primary set** aligns with the standing committees of the Romanian parliament (Educație, Sănătate, Apărare, Economie, Transporturi, Afaceri europene, Justiție, Muncă, Agricultură, Mediu, Cultură, Buget-finanțe, Administrație, Politică externă, Drepturile omului, plus "Other"). ~15 chips for a faceted UI. Initial population is regex from the agenda title ("Comisia pentru transporturi și infrastructură" → "Transporturi"). Cheap, deterministic, ~80% coverage.
- **Open secondary set** captures the long tail ("PISA testing", "ANL", "drumuri naționale", "RCA") that is too specific for committee-level grouping but valuable for fine search and topic clustering. Filled by an LLM pass (eventual, not day-1).
- Topic lives at the agenda item, not at the speech. Speeches inherit their parent's topic for retrieval.

**Why not free-only or closed-only.** Free-only produces 3,000 distinct labels and useless faceted search. Closed-only loses specificity ("Educație" alone is much weaker than "Educație" + "PISA"). The two-tier shape gives both useful facets and useful long-tail.

**Topic provenance.** Topic tags carry their own `extraction` block — `extractor: "regex@1"` for primary tags inferred from committee names, `extractor: "llm@..."` for the secondary refinement. Lets you trust/distrust them differently in legal vs. journalistic contexts.

### 9. `committee_synthesis` body — meeting as atom, no speeches

**Decision.** The body of a `committee_synthesis` is:

- `period: { start, end }` — the date range covered by the synthesis
- `committees: [{ name, chair, meetings: [{ date, joint_with, attendees, agenda, ... }] }]`

There is **no `activities[]` array, no speech turns**. The atomic unit of meaning is the committee meeting, not the speaker turn.

**Why the structural break from plenary.** The source genre is fundamentally different. A plenary stenogramma records every word said; a committee synthesis records *which committee met when, who attended, what was on the agenda, what was decided*. Forcing speech turns into this type would invent data the source does not contain. The shape mirrors the source.

**`joint_with` is first-class.** Joint committee sessions are common (sample: "Comisia pentru politică economică, reformă și privatizare în comun cu Comisia pentru buget, finanțe și bănci"). Shared sessions get one meeting record per primary committee with `joint_with` listing the partners. Not "optional metadata" — load-bearing for queries like "find all joint sessions of committees X and Y".

**`attendees[]` is per-meeting, not per-committee.** Politicians come and go across days. Each attendee is the full `Speaker` shape, so when `person_id` is backfilled, attendance becomes queryable too ("which committee meetings did Politician X attend in 2013?").

**Vote summaries are leaner than plenary votes.** Committee work records vote outcomes narratively: "a fost aprobat cu majoritate de voturi (6 voturi împotrivă și două abțineri)". The schema captures this as a `vote_summary` (one per agenda item) with `outcome`, `majority` ("majority | unanimous | tied"), and optional integer `against` / `abstain` / `amendments_passed` counts. Reusing the heavyweight plenary `vote` shape would lie about precision the source doesn't have.

**Substitutions are tracked.** "Domnul deputat X a fost înlocuit de domnul deputat Y" appears regularly. Captured as `substitutions: [{ absent: Speaker, substitute: Speaker }]` so attendance accountability is preserved.

### 10. `other` body — minimal, never silent

**Decision.** The body of an `other` document is:

```json
{
  "headings": [{ "level": 1, "text": "...", "line": 9 }, ...],
  "raw_markdown_excerpt": "first ~500 chars for snippet generation",
  "extraction": {
    "extractor": "fallback@1",
    "confidence": 0.0,
    "reason": "no_known_layout_matched | joint_session_unmodeled | special_protocol",
    "candidate_types": [{ "type": "plenary_stenogram", "score": 0.32 }, ...]
  }
}
```

**The contract is "never silent failure".** Every document classifies into one of the three types. `other` is the explicit "we recognize this exists but we don't know how to structure it yet" bucket. The `reason` field records *why* the document fell here. The `candidate_types` field records what the type-detector saw — so when joint sessions or oath ceremonies eventually get modeled as proper types, you can backfill with `WHERE document_type='other' AND extraction.reason='joint_session_unmodeled'` (filesystem walk; no full reclassification).

**Minimal structure is intentional.** `headings` (the markdown outline) gives search something to match. `raw_markdown_excerpt` is enough for snippet generation. The full text remains in the sidecar `.md` file. No claim of structure beyond what's verifiable.

**Resist type-explosion.** Sub-genres within plenary (budget debates, no-confidence votes, government question time) can probably stay inside `plenary_stenogram` with a `session.special_procedure` marker rather than spawning new top-level types. Promote a sub-genre to its own type only when the body shape genuinely diverges from the parent — as `committee_synthesis` does from plenary.

### 11. Storage, chunking, versioning

**Storage.** Sidecar JSON files only. `<basename>.json` next to `<basename>.pdf` and `<basename>.md` in the `pdfs/` directory. No SQLite registry; no flat projections; no S3 mirror at the JSON layer (yet).

**Why sidecar-only for now.** The first cut is the smallest cut that ships value. Elasticsearch will become the indexing layer when wired up — at that point, ES ingest produces the flat per-speech / per-vote / per-reference indices natively, and the canonical hierarchical JSON is the source feeding ingest. Building a parallel SQLite registry or `.jsonl` projections before ES exists is yak-shaving.

The sidecar file pattern composes cleanly with the existing pipeline: `monitorul-ii fetch` writes `<basename>.pdf`, `monitorul-ii convert` writes `<basename>.md`, the (future) `monitorul-ii extract` subcommand writes `<basename>.json`. Each step is independent, idempotent, and inspectable.

**Chunking — no field, natural units serve.** RAG chunking is a downstream concern. Speeches in the corpus range from ~100 to ~2,000 tokens — comfortably embeddable as-is for any modern embedding model with an 8k+ context window. Pre-chunking commits the canonical to one model's context window; the next model breaks all your chunks. Pre-chunking also commits to an overlap strategy that's embedding-model-specific.

The hierarchical JSON's natural units already give well-bounded passages: each speech, each interpellation, each committee meeting outcome paragraph. The Elasticsearch ingest pipeline reads these directly and produces per-speech embedding rows. Long-tail (a speech that exceeds the embedding model's context) is handled by the ingest pipeline's own sentence-split — not by the canonical schema.

**Versioning — orthogonal axes.** Two version fields, deliberately independent:

```json
{
  "schema_version": "1.0.0",                    // semver — bumped when fields change
  "extraction": {
    "extractor": "hybrid@1",                    // bumped when extraction quality changes
    "extracted_at": "2026-05-04T08:30:00Z",
    "extractor_versions": {
      "regex": "1.0.0",
      "speaker_parser": "0.1.0",
      "topic_classifier": null
    }
  }
}
```

- **`schema_version`** tells consumers what fields to expect. Bump major on breaking changes (field removed, type changed), minor on additive (new optional field), patch on clarifications. Migration is a script that reads all canonical JSONs, transforms, writes back.
- **`extraction.extractor`** tells consumers *what produced this content*. Same schema version, different extractor = same shape, possibly improved content. Re-extraction is a `WHERE extractor < ...` slice, applied as a filesystem walk over sidecar files.

These are independent because schema evolution and extraction quality evolve on different rhythms. Today's "speaker is `{raw, name, party_group}`" can become tomorrow's "speaker is `{raw, name, party_group, person_id}`" (additive minor schema bump) without touching extraction at all — just the field gets added with `null`s and backfilled when the registry exists.

## Schema revisions from corpus sampling (2025-05 → 2026-05)

The 1.0.0 design tree above was derived from three sample documents (one 2000-era committee synthesis, one 2013 Senate stenograma, one 2026 Camera Deputaților stenograma). Before treating the schema as production-ready, we randomly sampled 25 markdown files from the most recent 12 months (2025-05-04 → 2026-05-04) and audited each for fit. The audit surfaced patterns that the original three samples did not contain. Bumping `schema_version` to **1.1.0** (additive only — no breaking changes; consumers reading 1.1.0 records can ignore unfamiliar fields and continue).

The remainder of this section records the findings and the schema deltas they motivated, so the audit trail survives in the design record.

### Findings — plenary stenograms

**P-1. Joint sessions of Camera + Senat are common and have a structurally distinct shape.** Sample `2026-04-21_MO-PII-41-2026.md` is the joint debate on the 2026 state budget. Header reads `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI`. Two presiding chairs (one from each chamber). Combined attendance counted across both chambers (391 of 463 = senators + deputies). Voting tallies mix senators and deputies. Bills carry **parallel reference codes**, one per chamber (`L146/2026; PL-x 184/2026`).

The 1.0.0 schema put this in the `other` bucket with `reason: "joint_session_unmodeled"`. The shape is now common enough — and structurally divergent enough from a single-chamber plenary — to warrant promotion to its own type. **Add `document_type: "plenary_joint_session"`** as a fourth value in the discriminated union. Body shape is plenary-like with two `chair` lists and a `chambers_present[]` field; otherwise reuses the `agenda_items` / `interpellations` shapes.

**P-2. The current `metadata.chamber` field cannot represent a joint session.** The converter's frontmatter currently writes `chamber: "Camera Deputaților"` for the joint session sample, which is inaccurate. **Extend the enum** to `Camera Deputaților | Senatul | joint`. The extractor overrides the converter's frontmatter when it detects "ȘEDINȚE COMUNE" in the body.

**P-3. Bills that travel both chambers carry parallel reference codes.** The current schema models `agenda_items[].primary_reference: Reference | null` (singular). In joint sessions and budget bills, the agenda item carries both the Camera code (`PL-x N/Y`) and the Senate code (`L M/Y`) — they are *the same bill* observed from two procedural sides. **Change `primary_reference` to `primary_references: Reference[]`** (always an array, length 1 in the common case, length 2+ for cross-chamber items).

**P-4. The `agenda_items[].category` field needs an explicit enumeration; the survey surfaced 13 distinct values.** The 1.0.0 doc left this as a free string. Most modern sessions are not "lege ordinară debates" — they include declarații politice (entire opening blocks), commemoratives (`Alocuțiune cu prilejul...`), tacit adoptions, motion debates, appointment confirmations, regulation amendments, and procedural notes. **Document the enumeration** so extractors normalize:

- `bill_debate` — standard debate on a bill (the implicit default)
- `political_declarations` — `Declarații politice și intervenții ale deputaților` blocks (often the entire first hour of a session)
- `commemorative` — `Alocuțiune cu prilejul...`, `Moment de reculegere în memoria...`
- `final_vote_batch` — `Supunerea la votul final` items that resolve the deferred votes from earlier in the session (one item, many sub-votes)
- `procedural` — agenda approval, programme of work, deadline extensions, agenda modifications
- `tacit_adoption` — bills `Aprobate tacit, prin împlinirea termenului` (the constitutional silence-equals-adoption mechanism)
- `appointment` — `Numirea unor membri și supleanți...` to extra-parliamentary bodies (CSM, CNI, CCR, etc.)
- `motion` — `Prezentarea, dezbaterea și respingerea/adoptarea moțiunii simple/de cenzură`
- `committee_report_presentation` — `Prezentarea Raportului privind activitatea Comisiei...` periodic activity reports
- `legislative_transmission` — `Aprobarea transmiterii către Camera Deputaților, ca primă Cameră sesizată...` (passing a bill to the other chamber)
- `withdrawal` — `Aprobarea solicitării de retragere din procesul legislativ`
- `notification` — informational notes (`Notă pentru exercitarea... dreptului de sesizare a Curții Constituționale`, motion-deposit announcements)
- `regulation_amendment` — debates on the chamber's own internal `Regulament` (Senatului / Camerei Deputaților)

**P-5. The `agenda_items[].outcome` enum needs the same treatment.** Surveyed values:

- `adoptat` / `respins` — the common pair
- `adoptat_tacit` — outcome of the `tacit_adoption` category
- `retrimis` — `retrimitere la Comisia X` (returned to committee)
- `retras` — withdrawn from process
- `votul_final_deferred` — `rămas pentru votul final` (this session, batch-voted in the final-vote item)
- `vot_amânat` — `votul final se va da într-o ședință viitoare` (cross-session deferral; `defers_to` slot now genuinely needed)
- `informare` — informational items have no decision outcome
- `tăcere_legislativă` — never debated within the constitutional window (rare but observed)

**P-6. Reexaminations carry a stated reason.** The 1.0.0 schema treated `reexaminare` as a `category`; the survey shows reexaminations also state *why*: `reexaminare la solicitarea Președintelui României` or `ca urmare a Deciziei Curții Constituționale nr. X/Y`. **Add `agenda_items[].reexamination_reason`**: `presidential_request | constitutional_court | parliamentary_majority | null`.

**P-7. Vote count `not_voting` is a fourth state.** Joint-session and electronic-voting tallies regularly distinguish three counts plus an explicit "did not vote" group: `339 de voturi pentru, un vot contra, 13 colegi nu votează`. The 1.0.0 schema's `vote.counts` only models for/against/abstain. **Add `not_voting: int | null`** to `vote.counts`. This is a meaningful signal for accountability ("which deputies were absent during the budget vote") that the existing fields would silently aggregate into "abstain" — which would be wrong.

**P-8. PHCD references are common and need their own discriminator.** Sample `2025-09-26_MO-PII-109-2025.md` shows multiple `PHCD 41/2025`, `PHCD 56/2025`, `PHCD 57/2025` references — `Proiect de Hotărâre a Camerei Deputaților`, a chamber resolution distinct from a `lege` (different legal weight, no presidential promulgation). The 1.0.0 schema's `bill` type does not cover this. **Add `Reference.type: "chamber_resolution"`** with `prefix: "PHCD" | "PHS" | "PHCDS"`, `number`, `year`, `chamber`. Keeping it separate from `bill` matters because chamber resolutions and bills have different legal effects — a search for "all bills mentioning X" should not match resolutions.

**P-9. Motions (moțiune simplă, moțiune de cenzură) need to be referenceable as objects.** Currently the schema would render them as agenda titles only, with no structured handle. **Add `Reference.type: "motion"`** with `motion_type: "simplă" | "de cenzură"`, `subject: string`, `proposers: Speaker[]`. Lets you query "all motions tabled by group X" or "all motions against ministry Y".

### Findings — committee syntheses

**C-1. Multi-day committee meetings are reported as a single block, not per day.** Sample `2025-05-28_MO-PII-4c-2025.md` opens with "Comisia ... și-a desfășurat lucrările în zilele de **21**, **22**, **23** și **24 octombrie 2024**" — four consecutive working days reported in one narrative. The 1.0.0 schema models `meeting.date: date` (singular). **Change to `meeting.dates: date[]`** (always an array, length 1 in the simple case). When per-day attendance differs, each per-day roster gets its own meeting record; when the synthesis treats the days as one event with one roster, one record with multiple dates.

**C-2. Meetings have multi-window time intervals within a day.** Same sample: "în intervalele orare 8:30-12:00, respectiv 13:00-18:00". The 1.0.0 schema's `started_at` (singular) loses the second window. **Change to `meeting.time_windows: [{ start, end }]`** (always an array; legacy `started_at`/`ended_at` consumers can read the first window).

**C-3. Attendance has a three-way state, not just present/absent.** Sample `2025-08-12_MO-PII-28c-2025.md` has explicit `Prezent fizic` / `Prezent online` / `Absent` markers (and gendered `Prezentă fizic` / `Prezentă online`). The 1.0.0 schema's separate `attendees[]` / `absentees[]` / `substitutions[]` arrays force consumers to re-stitch each member's true state. **Replace the three arrays with a single `meeting.roster[]`**:

```json
{ "speaker": Speaker, "mode": "physical | online | absent | substituted",
  "substituted_by": Speaker | null }
```

One row per committee member, one true state each. The attendance-mode signal preserved naturally; queries like "members who attended online during the pandemic period" become trivial.

**C-4. Meetings have a `format` field analogous to plenary.** "în sistem mixt (prezență fizică și prin mijloace electronice)" is observed. **Add `meeting.format: "in_person | online | mixed"`** mirroring the plenary `session.format` field.

**C-5. Joint sub-committee work is plural, not singular.** Sample agenda items routinely state `fond comun cu Comisia X și Comisia Y` (two co-committees, sometimes three). The 1.0.0 schema's `agenda[].co_committee: string | null` is inadequate. **Change to `agenda[].co_committees: string[]`**.

**C-6. Committee role per agenda item is a critical missing field.** Each committee handles each bill in one of three roles: `fond` (substantive), `aviz` (advisory opinion only), `fond comun` (jointly substantive). This determines what the committee is *empowered* to do for that bill. **Add `agenda[].committee_role: "fond" | "aviz" | "fond_comun"`**. Distinct from `outcome` — role is "what the committee was asked to do", outcome is "what the committee decided".

**C-7. Committee output type per agenda item is also missing.** What the meeting *produced*: `raport`, `raport preliminar`, `raport suplimentar`, `aviz`, `amânare` (deferral). **Add `agenda[].output_type`**. When `output_type == "raport_preliminar"`, **also add `agenda[].for_committee: string | null`** identifying the committee that receives the preliminary report.

**C-8. Some meetings are documentation/consultation, others are decision-making.** Sample `2025-11-18_MO-PII-33c-2025.md` shows the same agenda items appearing on day 1 with a `– documentare și consultare` suffix and again on day 2 without (decision day). **Add `meeting.purpose: "documentare_consultare" | "dezbatere_decizie" | "aprobare_raport" | null`** so consumers can filter "which committees actually voted on this bill" vs. "which committees informally discussed it".

**C-9. Tabular committee summaries are a real sub-format.** Sample `2025-08-12_MO-PII-28c-2025.md` reports each committee meeting as a table (`Nr. crt | Număr PL-x | Titlu | Scopul sesizării | Rezoluție`) with a separate attendance table beneath, instead of the narrative format used in 2013. The schema fields above accommodate both — the columns map directly to `primary_references[0]`, `title`, `committee_role` / `output_type`, `outcome_text`. No new field needed; this is an extraction-pass observation: the parser must handle both tabular and narrative formats and emit the same structured records.

### Cross-cutting findings

**X-1. Speakers self-attribute party at the end of speeches.** Sample `2025-07-25_MO-PII-91-2025.md` has speakers signing off with "Niculina Stelea, senator AUR ales în Circumscripția nr. 42 București." Sometimes more reliable than the chair's introduction. No schema change needed — the `Speaker` shape already captures `party_group`; this is an extraction signal to combine with the chair's introduction.

**X-2. Sessions are split into parts with different chairs.** "Lucrările au fost conduse, în prima parte, de doamna X / A doua parte a ședinței a fost condusă de domnul Y" appears regularly. The 1.0.0 `session.chair: [Speaker, ...]` array holds the names but loses the segment boundary. Optional, additive: **add `session.chair_segments: [{ chair: Speaker, segment_label: string, started_at: time | null }] | null`**. Defer if extraction proves brittle; the basic `chair[]` array is sufficient for v1.1.0.

**X-3. Time allocations per parliamentary group are explicitly announced for major debates.** "Guvernului i se rezervă 30 de minute, ... PSD – 13 minute, AUR – 9 minute..." Could be captured as `agenda_items[].time_allocations: [{ group: string, minutes: int }] | null`. Defer to v1.2 — small value-to-implementation ratio.

### Summary of 1.0.0 → 1.1.0 deltas

| # | Delta | Why |
|---|---|---|
| 1 | New `document_type: "plenary_joint_session"` | Joint sessions are common and structurally distinct |
| 2 | `metadata.chamber` enum gains `"joint"` | Existing field cannot represent the joint case |
| 3 | `agenda_items[].primary_reference` → `primary_references[]` | Cross-chamber bills have parallel codes |
| 4 | `agenda_items[].category` enum documented (13 values) | Many real items are not bill debates |
| 5 | `agenda_items[].outcome` enum documented (8 values) | Need explicit "tacit adoption", "withdrawn", deferred-vs-cross-session-deferred |
| 6 | `agenda_items[].reexamination_reason` field added | Reexaminations carry a stated reason |
| 7 | `vote.counts.not_voting` field added | Fourth state in electronic voting |
| 8 | New `Reference.type: "chamber_resolution"` (PHCD) | Distinct legal weight from bills |
| 9 | New `Reference.type: "motion"` (simplă / de cenzură) | Need structured handle for motion debates |
| 10 | Committee `meeting.date` → `meeting.dates: []` | Multi-day blocks are common |
| 11 | Committee `meeting.started_at` → `meeting.time_windows: []` | Multi-window meetings are common |
| 12 | Committee `attendees[]` + `absentees[]` + `substitutions[]` → single `roster[]` with `mode` | Three-way attendance is the underlying truth |
| 13 | Committee `meeting.format` field added | Mirrors plenary `session.format` |
| 14 | Committee `agenda[].co_committee` → `co_committees: []` | Triple-committee bills exist |
| 15 | Committee `agenda[].committee_role` field added | Critical "fond/aviz/fond_comun" distinction missing |
| 16 | Committee `agenda[].output_type` field added | "Ce a produs ședința" is queryable signal |
| 17 | Committee `agenda[].for_committee` field added | Receiving committee for preliminary reports |
| 18 | Committee `meeting.purpose` field added | Documentare-vs-decizie sub-genre |
| 19 | Optional `session.chair_segments` slot reserved | Multi-part sessions; deferred implementation |

All 19 are additive — no field is removed or has its type changed in a breaking way (`primary_reference` → `primary_references[]` is technically breaking, but the migration is mechanical: wrap singletons in a 1-element array). Migration script reads every 1.0.0 sidecar JSON, transforms, writes back at 1.1.0. Documents that pre-date the survey can be extracted directly at 1.1.0.

## Consolidated schema reference

Common envelope every document carries:

```json
{
  "schema_version": "1.1.0",
  "document_id": "mo://2026/PII/48",
  "content_sha": "a3f9c1d2e4b8",
  "document_type": "plenary_stenogram | plenary_joint_session | committee_synthesis | other",
  "metadata": {
    "issue": "48",
    "year": 2026,
    "part": "II",
    "published": "2026-04-29",
    "chamber": "Camera Deputaților | Senatul | joint",
    "session": "SESIUNEA I ORDINARĂ – APRILIE 2026",
    "session_date": "2026-04-14",
    "legislature": "X"
  },
  "raw_markdown_path": "pdfs/2026-04-29_MO-PII-48-2026.md",
  "raw_pdf_path": "pdfs/2026-04-29_MO-PII-48-2026.pdf",
  "extraction": {
    "extractor": "hybrid@1",
    "extracted_at": "2026-05-04T08:30:00Z",
    "extractor_versions": { "regex": "1.0.0", "speaker_parser": "0.1.0", "topic_classifier": null },
    "confidence": 0.94
  },
  "body": { "...one of the four shapes below..." }
}
```

Reusable shapes:

```json
// Speaker — used everywhere a person appears
{
  "raw": "Domnul Marian Crușoveanu",
  "name": "Marian Crușoveanu",
  "title": "deputat",
  "role": "raportor",
  "party_group": "PNL",
  "person_id": null
}

// Reference (discriminated union — type-specific shape per discriminator)
// type = bill
{ "type": "bill", "prefix": "PL-x", "number": "257", "year": 2019,
  "secondary_year": null, "chamber_of_origin": "camera",
  "category": "ordinară", "raw": "PL-x 257/2019", "char_offsets": [120, 134] }

// type = law
{ "type": "law", "number": "295", "year": 2004,
  "subject": "regimul armelor și munițiilor",
  "raw": "Legea nr. 295/2004", "char_offsets": [0, 18] }

// type = oug | og
{ "type": "oug", "number": "23", "year": 2013, "issuer": "Guvern",
  "raw": "Ordonanța de urgență a Guvernului nr. 23/2013", "char_offsets": [0, 45] }

// type = court_decision
{ "type": "court_decision", "court": "Curtea Constituțională", "number": "492",
  "date": "2022-11-02", "published_in": "MO Partea I, nr. 106 din 7 februarie 2023",
  "raw": "...", "char_offsets": [0, 0] }

// type = constitution
{ "type": "constitution", "article": "73", "paragraph": "1",
  "raw": "art. 73 alin. (1) din Constituția României", "char_offsets": [0, 0] }

// type = regulation
{ "type": "regulation", "body": "Camera Deputaților", "article": "94",
  "paragraph": null, "raw": "art. 94 din Regulamentul Camerei Deputaților",
  "char_offsets": [0, 0] }

// type = eu_doc
{ "type": "eu_doc", "series": "COM", "year": 2013, "number": "146",
  "subseries": "final", "subject": null,
  "raw": "COM(2013) 146 final", "char_offsets": [0, 19] }

// type = treaty
{ "type": "treaty", "name": "Tratatul de la Lisabona", "signed_date": null,
  "raw": "...", "char_offsets": [0, 0] }

// type = chamber_resolution (added in 1.1.0)
{ "type": "chamber_resolution", "prefix": "PHCD", "number": "41", "year": 2025,
  "chamber": "Camera Deputaților",
  "raw": "PHCD 41/2025", "char_offsets": [0, 12] }

// type = motion (added in 1.1.0)
{ "type": "motion", "motion_type": "simplă | de cenzură",
  "subject": "Diplomația progresistă cu hashtaguri a pus agricultura României în pericol",
  "proposers": [Speaker, "..."],
  "raw": "...", "char_offsets": [0, 0] }

// SourceSpan — every extracted record carries one
{ "lines": [432, 458], "chars": [12480, 13205], "content_sha": "a3f9c1d2e4b8" }

// PerSectionExtraction — every extracted record carries one
{ "extractor": "regex@1", "confidence": 0.92, "source_span": SourceSpan }
```

### body for `plenary_stenogram`

```json
{
  "session": {
    "chair": [Speaker, "..."],
    "secretaries": [Speaker, "..."],
    "attendance": { "registered": 185, "total_seats": 330 },
    "quorum_met": true,
    "opened_at": "16:01",
    "closed_at": null,
    "format": "in_person | online | mixed",
    "special_procedure": null
  },
  "agenda_items": [
    {
      "ordinal": 4,
      "title": "...",
      "primary_references": [Reference, "..."],
      "category": "bill_debate | political_declarations | commemorative | final_vote_batch | procedural | tacit_adoption | appointment | motion | committee_report_presentation | legislative_transmission | withdrawal | notification | regulation_amendment",
      "outcome": "adoptat | respins | adoptat_tacit | retrimis | retras | votul_final_deferred | vot_amânat | informare | tăcere_legislativă",
      "reexamination_reason": "presidential_request | constitutional_court | parliamentary_majority | null",
      "pages_in_pdf": [4, 5, 6],
      "topics": { "primary": ["Transporturi"], "secondary": ["drumuri-naționale"] },
      "activities": [
        {
          "type": "procedural",
          "actor": Speaker,
          "text": "...",
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        },
        {
          "type": "speech",
          "speaker": Speaker,
          "text": "...",
          "references_mentioned": [Reference, "..."],
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        },
        {
          "type": "vote",
          "motion_text": "...",
          "motion_type": "procedural | amendment | final | item_adoption | agenda_approval | urgency_procedure | report_approval",
          "voting_method": "electronic | show_of_hands | secret_ballot | nominal | by_acclamation",
          "timing": "live | deferred",
          "counts": { "for": 123, "against": 6, "abstain": 2, "not_voting": 13, "total_voting": 131 },
          "outcome": "approved | rejected | tied | deferred | no_quorum",
          "quorum_announced": null,
          "proposed_by": Speaker,
          "nominal_breakdown": null,
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        },
        {
          "type": "narrator",
          "text": "(Aplauze.)",
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        },
        {
          "type": "deferral",
          "text": "Aceasta rămâne pentru votul final.",
          "defers_to": null,
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        }
      ],
      "extraction": PerSectionExtraction
    }
  ],
  "interpellations": [
    {
      "questioner": Speaker,
      "addressed_to": "Ministerul Educației și Cercetării",
      "addressed_to_normalized": null,
      "interpellation_number": "1.172B",
      "topic": "...",
      "question_text": null,
      "response": null,
      "response_deferred": true,
      "source_span": SourceSpan,
      "extraction": PerSectionExtraction
    }
  ]
}
```

### body for `committee_synthesis`

```json
{
  "period": { "start": "2013-04-08", "end": "2013-04-11" },
  "committees": [
    {
      "name": "Comisia pentru politică economică, reformă și privatizare",
      "chair": Speaker,
      "meetings": [
        {
          "dates": ["2013-04-08"],                         // 1.1.0: was singular
          "joint_with": ["Comisia pentru buget, finanțe și bănci"],
          "presider": Speaker,
          "format": "in_person | online | mixed",          // 1.1.0: added
          "purpose": "documentare_consultare | dezbatere_decizie | aprobare_raport | null",  // 1.1.0: added
          "time_windows": [{ "start": "08:30", "end": "12:00" },
                           { "start": "13:00", "end": "18:00" }],  // 1.1.0: was started_at
          "roster": [                                      // 1.1.0: replaces attendees+absentees+substitutions
            { "speaker": Speaker, "mode": "physical | online | absent | substituted",
              "substituted_by": Speaker }
          ],
          "guests": [{ "name": "Claudiu Doltu", "title": "secretar de stat",
                       "organization": "Ministerul Finanțelor Publice" }],
          "agenda": [
            {
              "ordinal": 1,
              "title": "Cererea de reexaminare a Legii pentru aprobarea OUG nr. 93/2012...",
              "primary_references": [Reference, "..."],    // 1.1.0: pluralized for consistency with plenary
              "co_committees": ["Comisia pentru industrii și servicii",
                                "Comisia juridică, de disciplină și imunități"],  // 1.1.0: pluralized
              "committee_role": "fond | aviz | fond_comun",   // 1.1.0: added
              "output_type": "raport | raport_preliminar | raport_suplimentar | aviz | amânare",  // 1.1.0: added
              "for_committee": "Comisia pentru muncă și protecție socială",  // 1.1.0: when output_type=raport_preliminar
              "outcome_text": "...",
              "vote_summary": {
                "outcome": "approved | rejected | deferred",
                "majority": "majority | unanimous | tied",
                "for": null,
                "against": 6,
                "abstain": 2,
                "amendments_passed": null
              },
              "source_span": SourceSpan,
              "extraction": PerSectionExtraction
            }
          ],
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        }
      ],
      "source_span": SourceSpan,
      "extraction": PerSectionExtraction
    }
  ]
}
```

### body for `plenary_joint_session`

Same shape as `plenary_stenogram` with two differences:

- `session.chair[]` carries chairs from both chambers (typically the Senate president and the Chamber of Deputies president, often with vice-presidential substitutes).
- New `session.chambers_present: ["Camera Deputaților", "Senatul"]` field.
- `session.attendance.total_seats` is the combined chamber count (typically 463 = 333 deputies + 130 senators; varies by mandate).
- Agenda items routinely carry parallel `primary_references` (one Camera code, one Senate code).

```json
{
  "session": {
    "chair": [Speaker, "..."],
    "chambers_present": ["Camera Deputaților", "Senatul"],   // 1.1.0: added for joint sessions
    "secretaries": [Speaker, "..."],
    "attendance": { "registered": 391, "total_seats": 463 },
    "quorum_met": true,
    "opened_at": "21:06",
    "closed_at": null,
    "format": "in_person | online | mixed",
    "special_procedure": "buget_de_stat | declaratie_solemna | mesaj_prezidential | null"
  },
  "agenda_items": [
    {
      "ordinal": 2,
      "title": "Dezbateri asupra Proiectului Legii bugetului de stat pe anul 2026",
      "primary_references": [
        { "type": "bill", "prefix": "L", "number": "146", "year": 2026,
          "chamber_of_origin": "senat", "raw": "L146/2026", "char_offsets": [0, 9] },
        { "type": "bill", "prefix": "PL-x", "number": "184", "year": 2026,
          "chamber_of_origin": "camera", "raw": "PL-x 184/2026", "char_offsets": [11, 24] }
      ],
      "category": "bill_debate",
      "outcome": "adoptat",
      "pages_in_pdf": [2, 3, 4, 5, 6, 7, 8, 9, 14, 15, "..."],
      "topics": { "primary": ["Buget-finanțe"], "secondary": [] },
      "activities": [ /* same shape as plenary_stenogram */ ],
      "extraction": PerSectionExtraction
    }
  ],
  "interpellations": []   // joint sessions typically have no interpellation block
}
```

### body for `other`

```json
{
  "headings": [
    { "level": 1, "text": "...", "line": 9 },
    { "level": 2, "text": "...", "line": 22 }
  ],
  "raw_markdown_excerpt": "first ~500 chars for snippet generation",
  "extraction": {
    "extractor": "fallback@1",
    "confidence": 0.0,
    "reason": "no_known_layout_matched | joint_session_unmodeled | special_protocol",
    "candidate_types": [
      { "type": "plenary_stenogram", "score": 0.32 },
      { "type": "committee_synthesis", "score": 0.08 }
    ]
  }
}
```

## Backfill paths

Every nullable identity field in the schema is a deliberate slot for a future registry. They can be backfilled without re-extracting structure — additive minor schema bump, single-column update across stored JSON.

| Field | Registry | When |
|---|---|---|
| `Speaker.person_id` | `persons` registry — canonical name + alternate-name forms + birth year + mandate windows | After enough corpus exists to make ID resolution worthwhile (~hundreds of plenary docs extracted) |
| `Reference.{bill,law,oug,...}_id` | `legislation` registry, keyed by `(type, number, year)` | After extraction of references is stable across the corpus |
| `Interpellation.addressed_to_normalized` | `ministries` registry — handles renames, mergers, splits | After an LLM pass identifies the canonical ministry name set |
| `topics.secondary[]` | LLM topic classifier output | Always populated by LLM; never has a "registry" — but the closed `primary` set is stable |
| `vote.defers_to` / `resolves` | Cross-document linker pass | After both deferral and final-vote extraction is stable |

## Build order

1. **Type detector.** Cheap regex on issue suffix + body markers; classify all converted MDs into the four buckets (`plenary_stenogram | plenary_joint_session | committee_synthesis | other`). Detect joint sessions via `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI` header. Sanity check the distribution against expected ratios (~70% plenary, ~25% committee, ~3% joint, ~2% other).
2. **`plenary_stenogram` extractor.** Covers the bulk of the queryable corpus. Speaker parser, agenda enumeration with the 13-value `category` enum, reference regex pack including PHCD and motion types, vote detector with deferred-handling and `not_voting` count, interpellation block parser. Topics primary populated from agenda titles via regex.
3. **`plenary_joint_session` extractor.** Reuses most of the plenary extractor; key additions are dual-chair detection, `chambers_present[]` population, and parallel-reference parsing in agenda titles (e.g., `L146/2026; PL-x 184/2026`).
4. **`committee_synthesis` extractor.** Meeting-as-atom with `dates[]` and `time_windows[]`, unified `roster[]` with `mode` per member, `committee_role` and `output_type` per agenda entry, narrative vote summaries. Must handle both narrative and tabular sub-formats.
5. **`other` fallback.** Write the audit-trail JSON for everything that doesn't match. Confirm zero documents fail to classify.
6. **(Later)** Person registry → backfill `person_id` across all `Speaker` instances.
7. **(Later)** Legislation registry → backfill `*_id` on `Reference` instances.
8. **(Later)** Ministry registry → backfill `addressed_to_normalized` on interpellations.
9. **(Later)** LLM topic classifier → populate `topics.secondary`.
10. **(Later)** Cross-document linker → populate `vote.defers_to` and `resolves` between deferral activities and final-vote items.
11. **(Later)** Elasticsearch ingest. Denormalize speeches, interpellations, votes into ES indices for fielded search and embedding. The hierarchical JSON is the source; ES ingest produces flat row projections natively.
