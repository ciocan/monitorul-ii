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

## Schema revisions from broader corpus sampling (n=50, full corpus)

After 1.1.0 was drafted from 25 docs in the most recent 12 months, we audited a wider, longer-period sample: **50 random documents from the full corpus** of 1,212 markdown files spanning 2013-06 → 2026-04. This pass surfaced patterns the recent-only sample missed — older formats, rarer procedural genres, and one entire document type that doesn't appear in 2025-2026 routine business but is structurally distinct.

Bumping `schema_version` to **1.2.0** (additive only; consumers reading 1.2.0 records can ignore unfamiliar fields). The audit findings are recorded below in the same "P-N" / "C-N" / "X-N" format as the v1.1.0 section, so future contributors can trace each delta to the document that motivated it.

### Findings — fundamentally new document type

**P2-1. `R`-suffix issues are external reports reproduced verbatim, not stenograms.** Sample `2014-01-21_MO-PII-3R-2014.md` is the CSAT (Consiliul Suprem de Apărare a Țării) annual activity report for 2012, published in MO Partea II at issue `3/R/2014`. Header reads `(RAPOARTE DE ACTIVITATE)` instead of `(STENOGRAMA)`. Note in body: "Raportul... este reprodus în facsimil." The body is the report itself — its own table of contents (`CUPRINS`), chapters (`CAPITOLUL I` through `CAPITOLUL XII`), perspectives section. No debate, no votes, no agenda. The "session" frame is just "Parliament received this report at this joint session on this date".

This genre is a fundamentally distinct fifth document type. The constitutional bodies that report annually to Parliament (CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului, etc.) all publish through this channel. They show up rarely — perhaps a dozen per year — but each is procedurally distinct and structurally divergent from the other four types. **Add `document_type: "report_facsimile"`** with a minimal body shape that records the report metadata and a heading outline; the full text remains in the sidecar markdown.

The 1.0.0/1.1.0 schema would force these into `other` with a generic audit reason, losing the queryable signal "give me all CSAT annual reports" and "what did SRI report between 2015 and 2020".

### Findings — plenary stenograms

**P2-2. Lowercase `b###/YYYY` is a third bill prefix, missed in the v1.1.0 enum.** Samples `2013-12-23_MO-PII-165-2013.md` (`b851/2013`, `b618/2013`) and `2022-08-11_MO-PII-106-2022.md` (`b492/2022`, `b493/2022`, `b494/2022`) show this prefix used for initial-stage proposals before they receive a Pl-x/PL-x code in the active chamber. **Extend `bill.prefix` enum to include `"b"`**.

**P2-3. `HP` (Hotărârea Parlamentului României) is a fourth chamber-resolution prefix.** Sample `2025-10-13_MO-PII-117-2025.md` (joint session) routinely references "Hotărârea Parlamentului României nr. 5/2025" — a joint resolution of both chambers, distinct from chamber-specific PHCD (Camera) or PHS (Senate). **Extend `chamber_resolution.prefix` enum to include `"HP"`**, and update the field semantics so `HP` resolutions are scoped to `chamber: "joint"` rather than a single chamber.

**P2-4. Party-affiliation changes are recorded as agenda items across all eras.** Resignations, expulsions, party switches, and "neafiliat" activations appear consistently from 2013 (`2013-12-23_MO-PII-165-2013.md`, `2013-06-04_MO-PII-69-2013.md`) through 2022 (`2022-04-12_MO-PII-44-2022.md`). They're procedurally important — they are *the* events that produce the per-occurrence party-attribution timeline the schema commits to. **Add `agenda_items[].category: "party_membership_change"`**. The agenda title ("Domnul senator X informează plenul Senatului asupra demisiilor din Grupul parlamentar Y...") is the source the future person registry will use to derive party-timeline transitions.

**P2-5. EU subsidiarity-check procedure is a distinct agenda category.** Sample `2013-12-23_MO-PII-165-2013.md` item 7 has "Dezbaterea și adoptarea unui proiect de hotărâre privind exercitarea controlului de subsidiaritate și proporționalitate" with an attached COM(YYYY) NNN reference. The procedure is constitutionally separate from regular bill debates (it implements Protocol 2 of the Lisbon Treaty). The 1.1.0 `category` enum would force this under `bill_debate` or `regulation_amendment`, both inaccurate. **Add `agenda_items[].category: "subsidiarity_check"`**.

**P2-6. Foreign-leader address sessions are their own genre.** Sample `2021-06-22_MO-PII-96-2021.md` is a joint session held purely to receive Israeli President Reuven Rivlin's address. Single agenda item, ceremonial elements (anthems of both countries), no debate, no vote. The 1.1.0 schema put it in `commemorative` — wrong, since these are state-visit protocol events with foreign sovereign as speaker, not commemorations of past events. **Add `agenda_items[].category: "foreign_address"`**.

**P2-7. Seat-vacancy declarations are procedurally distinct.** Sample `2022-04-12_MO-PII-44-2022.md` item 5: "Dezbaterea Proiectului de hotărâre privind vacantarea unui loc de deputat (ca urmare a decesului)". Formal declaration after a deputy/senator dies, resigns, or accepts an incompatible position (judge, minister). 1.1.0 would force this under `procedural` or `appointment`, both wrong. **Add `agenda_items[].category: "seat_vacancy"`**.

**P2-8. Joint declarations of Parliament are distinct from individual MP declarații politice.** Sample `2025-10-13_MO-PII-117-2025.md` item 2: "Prezentarea și adoptarea Proiectului Declarației Parlamentului României cu ocazia Zilei internaționale a nonviolenței". A joint declaration adopted by both chambers, qualitatively different from individual deputies' political declarations or chamber-specific resolutions. **Add `agenda_items[].category: "parliamentary_declaration"`**.

**P2-9. Delegation appointments are a recurring sub-genre of `appointment`.** Joint sessions routinely modify "componența nominală și conducerea Delegației permanente a Parlamentului României la [Adunarea Parlamentară a NATO/OSCE/...]". Sample `2025-10-13_MO-PII-117-2025.md` items 4-9 are all of this kind. Could fold under `appointment`; for query usability, **add `agenda_items[].category: "delegation_membership"`** as a distinct category. Document title carries the receiving body name.

### Findings — interpellations vs questions

**P2-10. Oral questions and formal interpellations are procedurally different and appear as separate agenda items.** Sample `2021-12-24_MO-PII-190-2021.md` items 11 and 12: "Răspunsuri orale la întrebările adresate membrilor Guvernului" (oral answers to questions, item 11) and "Prezentarea interpelărilor adresate Guvernului" (presentation of formal interpellations, item 12). Romanian parliamentary procedure distinguishes:

- **`întrebare`** — short-form question, expects oral answer in the same session
- **`interpelare`** — long-form formal interpellation, can lead to follow-up debate, often answered in writing later

The 1.1.0 schema's `interpellations[]` block conflates both. The procedural difference matters for accountability ("which ministers answered orally vs deferred to writing"). **Add `interpellations[].genre: "întrebare | interpelare"`** to the existing block — preferring this over splitting into two top-level arrays because the data shape is otherwise identical.

### Findings — committee syntheses

**C2-1. The roster carries an intra-committee role per member.** Sample `2022-02-25_MO-PII-5c-2022.md` lists each member as `Bende Sándor – președinte, Grupul parlamentar al UDMR – prezent în sală`. The roles `președinte`, `vicepreședinte`, `secretar`, `membru` are stable across meetings (until membership changes) and are queryable signal — "who chairs each committee" is a real accountability question. **Add `roster[].intra_committee_role: "președinte | vicepreședinte | secretar | membru"`**.

**C2-2. Attendance terminology varies (`prezent fizic` vs `prezent în sală`).** Both mean "present in person". Pure extraction normalization concern — the schema's `mode: physical` is fine; the regex pack must handle both phrasings.

### Findings — cross-cutting

**X2-1. `SESIUNE EXTRAORDINARĂ` is a distinct session type.** Sample `2022-08-11_MO-PII-106-2022.md` runs as "SESIUNE EXTRAORDINARĂ – IULIE 2022" (extraordinary session called between regular sessions). The 1.1.0 schema's `metadata.session` is a free string that holds this — but for faceted search, **add `metadata.session_type: "ordinary | extraordinary" | null`**, derivable from the existing `session` string. Optional, additive.

**X2-2. Speaker delivery mode is occasionally annotated.** Sample `2022-04-12_MO-PII-44-2022.md` shows `Domnul Alfred-Robert Simonis (din sală):` — speaker spoke from the floor rather than the tribune. **Optional addition: `speech.delivery_mode: "tribune | from_floor | written | online" | null`**. Defer to v1.3 if extraction proves brittle; relatively rare.

**X2-3. Parliament's session naming uses Roman numerals (`A II-A`)** which sometimes appears with extra spacing (`SESIUNEA  A II-A`, double space). Extraction normalization concern; no schema change needed.

**X2-4. Older PDFs have corrupted diacritics from the PDF→MD pipeline.** Sample `2014-01-21_MO-PII-3R-2014.md` shows "activit��ii" instead of "activităților", "��rii" instead of "țării". This is upstream of the schema (the converter uses pymupdf4llm). Schema must tolerate it; downstream search may benefit from a "fold-mojibake" normalization pass. No schema change.

### Summary of 1.1.0 → 1.2.0 deltas

| # | Delta | Why |
|---|---|---|
| 1 | New `document_type: "report_facsimile"` | `R`-suffix issues are externally-produced reports reproduced verbatim, not Parliament debates |
| 2 | `bill.prefix` enum gains `"b"` | Initial-stage proposals before they get an active-chamber code |
| 3 | `chamber_resolution.prefix` enum gains `"HP"` | Joint resolutions of both chambers are distinct from PHCD/PHS |
| 4 | `agenda_items[].category` gains `party_membership_change` | Resignations/expulsions/switches/neafiliat — the events that produce the per-occurrence party timeline |
| 5 | `agenda_items[].category` gains `subsidiarity_check` | EU Protocol-2 procedure, constitutionally distinct from bill debates |
| 6 | `agenda_items[].category` gains `foreign_address` | State-visit protocol events with a foreign sovereign as speaker |
| 7 | `agenda_items[].category` gains `seat_vacancy` | Formal vacancy declarations after death/resignation/incompatibility |
| 8 | `agenda_items[].category` gains `parliamentary_declaration` | Joint declarations of both chambers, distinct from individual MP declarations |
| 9 | `agenda_items[].category` gains `delegation_membership` | Recurring sub-genre of appointments to international parliamentary assemblies |
| 10 | `interpellations[].genre: "întrebare | interpelare"` field added | Procedurally distinct objects conflated by 1.1.0 |
| 11 | `roster[].intra_committee_role` field added | Stable per-member committee role; needed for "who chairs what" queries |
| 12 | `metadata.session_type` field added | Ordinary vs extraordinary sessions; derivable from existing `session` string |
| 13 | `speech.delivery_mode` slot reserved (optional) | `(din sală)` and similar annotations; defer implementation to v1.3 |

All 13 are additive. Combined `category` enum after v1.2.0 reaches 18 values; the discriminator pattern continues to scale as long as we resist creating new top-level types for things that fit in an existing body shape.

## Schema revisions from year-bounded sampling (n=50, 2020-05 → 2026-05)

After 1.2.0 settled, we ran another audit pass: **50 random documents from the most recent 10 years**. Because the corpus has gaps before 2020 (the converter started operating in late 2020), the sample concentrated in 2020-2026 — exactly the pandemic-era / hybrid-session period whose sub-format quirks the prior passes underweighted. The audit surfaced new procedural genres absent from earlier samples (legislature openings, government investitures, Government Hour debates, oath-taking ceremonies, end-of-session unanswered-question lists) and confirmed that two slots reserved as "deferred" in v1.1.0 (`speech.delivery_mode`, `session.chair_segments[]`) are recurrent enough to promote to implemented status.

Bumping `schema_version` to **1.3.0** (additive only — no breaking type changes; one previously-singular committee field gets pluralized in line with the v1.1.0 pattern, and one previously-flat array becomes an array of structured objects with a mechanical migration). Findings recorded in the same `P3-N` / `C3-N` / `X3-N` format.

### Findings — plenary stenograms

**P3-1. End-of-session unanswered-question lists are a structurally distinct genre, not stenograms.** Sample `2021-09-16_MO-PII-129-2021.md` and `2026-02-13_MO-PII-8-2026.md` are full files whose header reads `LISTA ÎNTREBĂRILOR ADRESATE DE CĂTRE DEPUTAȚI/SENATORI ... LA CARE NU S-A PRIMIT RĂSPUNS`. Body is a flat catalogue of written questions, grouped by minister-addressee, each with an MP signatory and a registration number. No agenda items, no votes, no speakers. The `(STENOGRAMA)` body marker is absent, so the v1.0.0 detection rule routes them to `other` even though they share the outer `DEZBATERI PARLAMENTARE` header. ≥20 such documents corpus-wide.
**Add `document_type: "question_register"`** as a sixth value with body `{ session_label, chamber, questions: [{ ordinal, addressee: { ministry, name, role }, questioner: Speaker, registration_number, registration_date, topic, question_text, source_span, extraction }] }`. Detection rule: header contains `LISTA ÎNTREBĂRILOR ADRESATE` AND body lacks `(STENOGRAMA)`. Misrouting these to `other` loses the queryable signal "all unanswered questions filed during legislature X by minister Y", which is a direct accountability question.

**P3-2. Pre-vote system-check votes are not substantive votes.** Sample `2021-03-17_MO-PII-31-2021.md` line 182 (chair: "Și vă rog să facem un vot de control înainte de a face votul pe acest proiect.") and `2021-03-26_MO-PII-34-2021.md` line 46 ("Vă propun să facem un vot de verificare, să vedem dacă funcționează sistemul.") show a recurring chair-driven pre-vote test of the electronic voting hardware / quorum. 318+ corpus hits for `vot de control` / `vot de verificare`. v1.2.0 would aggregate these as legitimate `procedural` votes — polluting per-MP voting records with hardware tests.
**Add `vote.motion_type: "system_check"`**. Records the chair's voting-system test; carries counts but is *not* a substantive decision and should be filtered out of accountability rollups by default.

**P3-3. Telephone roll-call is a sixth voting method.** Sample `2021-12-31_MO-PII-197-2021.md` line 44 (agenda item titled `Apel nominal telefonic`) and line 74 (votes computed by combining electronic-hybrid plus telephone roll-call within a single tally) show pandemic-era hybrid voting. Procedurally distinct from the existing `nominal` value: different timing, different logistics, different proxy for "in chamber".
**Extend `vote.voting_method`** with `"telephone_roll_call"`. Preferred over generalizing `nominal` into sub-types because it lets v1.2.0 records continue to validate; consumers can downcast if they want.

**P3-4. Government investiture / vote-of-confidence is constitutionally distinct from regular `motion` items.** Sample `2025-01-17_MO-PII-6-2025.md` item 2: "Prezentarea, dezbaterea și acordarea votului de încredere al Parlamentului asupra programului și întregii liste a Guvernului". 219+ corpus hits for `votului de încredere` / `moțiune de cenzură` / `învestire`. v1.2.0's `motion` covers *the referenced object* (the motion text), not the *agenda category*: investiture sessions have no preceding motion at all — they are unique constitutional events under articles 113-114.
**Add `agenda_items[].category: "government_confidence"`** with sub-flag `confidence_type: "investitură | cenzură | angajare_răspundere | demitere"`. Investiture and cenzură are inverse procedures with the same legal effect (forming / dismissing a government); engagement-of-responsibility (`angajare_răspundere`) is the third constitutional invocation — all three need the same agenda discriminator.

**P3-5. "Ora Guvernului" (Government Hour) is a recurring procedural format distinct from interpellations.** Sample `2023-10-30_MO-PII-139-2023.md` item 4: "Ora Guvernului – Dezbateri politice cu participarea ministrului apărării naționale, domnul Angel Tîlvăr, la solicitarea Grupului parlamentar al Uniunii Salvați România". 63+ corpus files match. The format is a parliamentary-group-requested debate with a specific minister attending in person — distinct from the `interpellations[]` block (single-MP→ministry pairs, often deferred to writing) and from regular `bill_debate` (no bill).
**Add `agenda_items[].category: "government_hour"`** with optional slots `requested_by_group: string` and `topic: string`. Forcing this under `procedural` or into `interpellations[]` loses the structural distinction (different rights, different output, no formal answer-deferral mechanism).

**P3-6. Oath-taking ceremonies are distinct procedural events, not appointments.** Sample `2024-08-01_MO-PII-93-2024.md` item 5 ("Depunerea jurământului de credință față de țară și popor de către domnul deputat Marius Vulcan") and `2025-10-13_MO-PII-117-2025.md` items 17-18 (oath for new SIE/SRI control-commission members). 437+ corpus hits.
**Add `agenda_items[].category: "oath_taking"`**. Distinct from `appointment` because (a) the swearing-in often happens days/months after the appointment vote, in a different document; (b) committee-membership oaths file under "Comisia parlamentară" not "Numirea". Agenda title pattern: `Depunerea jurământului ... de către [Speaker], în calitate de [Role]`.

**P3-7. Mandate validation for replacement deputies is the procedural complement of `seat_vacancy`.** Sample `2022-04-20_MO-PII-48-2022.md` item 3 ("Dezbaterea Proiectului de hotărâre privind validarea unui mandat de deputat") and `2024-08-01_MO-PII-93-2024.md` item 4. 199+ corpus hits. After a deputy/senator dies, resigns, or accepts an incompatible position (triggering `seat_vacancy`), the next candidate on the party list is validated through this distinct procedure.
**Add `agenda_items[].category: "mandate_validation"`**. v1.2.0's `seat_vacancy` covers the loss; the enum has nothing for the gain. The replacement is *not* appointed (so `appointment` is wrong) — they inherit the seat by election-list seniority. This is the event that produces a Speaker turning into a corpus-presence and is load-bearing for the future person registry's mandate-window tracking.

**P3-8. Solemn joint sessions are a specific `special_procedure` value, not generic declarations.** Sample `2022-12-27_MO-PII-159-2022.md` and `2025-12-15_MO-PII-160-2025.md` are both `Ședință solemnă comună consacrată aniversării zilei de 1 Decembrie` — annual National Day solemn sessions. The 1.1.0 `session.special_procedure` enum has `declaratie_solemna` which is a different event (declaration adoption, not anniversary observance).
**Extend `session.special_procedure`** with `"sedinta_solemna"` (anniversary / state-protocol observance) and — once-per-legislature but structurally unique — `"deschiderea_legislaturii"` (legislature-opening session, with `președinte senior` presiding and the constitution of the Validation Commission as a unique sub-event, observed in `2024-12-31_MO-PII-149-2024.md`). Solemn sessions have specific protocol elements (national anthem performed by an invited choir, clergy speakers, former presidents in attendance) that are *queryable* signals.

### Findings — committee syntheses

**C3-1. `studiu` and `proiect_de_opinie` are the dominant outputs of preparatory committee work.** Sample `2021-05-10_MO-PII-14c-2021.md` lines 105-116 (all six bills marked `– studiu`), `2021-06-22_MO-PII-21c-2021.md` lines 83-91 (same), `2024-06-17_MO-PII-17c-2024.md` line 46 (`examinare în fond, proiect de opinie`). 224+ corpus hits for `– studiu` / `studiu individual`. v1.2.0's `output_type` enum (`raport | raport_preliminar | raport_suplimentar | aviz | amânare`) miscategorizes these as `amânare` — but `studiu` is *active* preparatory review, not deferral. `proiect_de_opinie` is the EU-document equivalent — committees produce these for COM(YYYY) X / JOIN(YYYY) X under Protocol-2 subsidiarity check.
**Extend `agenda[].output_type`** to add `"studiu"` and `"proiect_de_opinie"`. Final enum: `raport | raport_preliminar | raport_suplimentar | aviz | studiu | proiect_de_opinie | amânare`.

**C3-2. Cross-chamber joint committee meetings exist; `joint_with[]` needs the chamber discriminator.** Sample `2023-02-21_MO-PII-6c-2023.md` lines 27-29: "Comisia pentru politică economică, reformă și privatizare și Comisia pentru industrii și servicii **din Camera Deputaților** împreună cu Comisia economică, industrii și servicii și Comisia pentru energie, infrastructură energetică și resurse minerale **din Senat** și-au început lucrările ședinței comune". v1.2.0's `joint_with: string[]` is too flat: within-chamber joint sessions (Camera ∩ Camera) are common; cross-chamber joint sessions are procedurally very different (the only mechanism for joint Camera+Senat committee work outside formal "Comisia comună" structures).
**Promote `joint_with[]` items from string to object**: `[{ name: string, chamber: "camera | senat" }]`. Mechanical migration (wrap each existing string with a derived chamber). This is the only type-shape change in 1.3.0.

**C3-3. Special / inquiry / ad-hoc committees are structurally distinct from permanent committees.** Sample `2023-05-29_MO-PII-15c-2023.md` items 22 (`Comisia specială ... pentru automatizare și viitorul muncii`) and 24 (`Comisia parlamentară de anchetă privind achizițiile publice ... în sectorul sanitar`); `2025-12-18_MO-PII-38c-2025.md` item 19 (`Comisia specială comună a Camerei Deputaților și Senatului ... în domeniul ... violenței domestice`); `2025-10-13_MO-PII-117-2025.md` item 4 (`Comisia specială comună ... pentru combaterea traficului de persoane`). v1.2.0 treats all committees as one kind; the metadata that varies (constitution date, sunset date, single-subject scope, joint-Camera-Senat composition) is not currently representable.
**Add `committee.kind: "permanent | special | inquiry | special_joint | inquiry_joint"`**. Special and inquiry committees are time-limited, single-subject, and produce only one kind of output (final report on the subject they were created to examine). Distinguishing them enables queries like "all reports of inquiry committees" or "current special joint committees".

### Findings — cross-cutting

**X3-1. `JOIN(YYYY) N` is a fifth EU-document series, missing from the v1.2.0 `eu_doc.series` enum.** Sample `2026-04-24_MO-PII-45-2026.md` line 95 (`JOIN(2025) 2`); 5+ corpus files. JOIN documents are formally co-issued by the Commission and the EU High Representative for Foreign Affairs and Security Policy; subsidiarity-check Hotărâri reference them just like COM documents.
**Extend `eu_doc.series`** from `COM | JOCE | REG | DIR` to add `"JOIN"`. (`SWD` — Staff Working Document — was not seen in this sample but is plausible; deferred until observed.)

**X3-2. Promote `speech.delivery_mode` from deferred to implemented.** v1.2.0 reserved this slot but flagged it as rare. The 50-doc sample shows it is *universal* across the 2020-2022 pandemic-era hybrid sessions — every speaker turn carries `(de la tribună)` / `(prin audioconferință)` / `(din sală)` annotation in `2020-09-25_MO-PII-106-2020.md` lines 75-95 and `2021-03-02_MO-PII-19-2021.md`. The pattern persists post-pandemic in 2022-2024 (e.g., `2022-04-20_MO-PII-48-2022.md` line 63: `_(din sală)_`). Skipping until v1.3 was the right call in v1.2.0; promoting now closes a multi-year corpus-wide gap.
**Add `agenda_items[].activities[].speech.delivery_mode: "tribune | from_floor | written | online | from_balcony" | null`**. The `from_balcony` value is rare but observed when foreign delegations or visitors are seated in the balcony ("domnul senator X (de la balcon)"). Optional — null when not annotated, which becomes the implicit default for pre-2020 plenaries.

**X3-3. Promote `session.chair_segments[]` from reserved to implemented.** v1.1.0 reserved this slot; the new sample shows mid-session chair swaps are routine. `2025-10-13_MO-PII-117-2025.md` lines 81-83 (entire prezidiu changes between first and final parts) and `2021-03-26_MO-PII-34-2021.md` line 36 (joint session with two chairs from two chambers, plus secretary swaps) confirm prevalence. The flat `session.chair: Speaker[]` representation loses the segment boundary that matters for accountability — the chair *at the moment* of a contested vote is part of the procedural record.
**Add `session.chair_segments: [{ chair: Speaker, secretaries: Speaker[], segment_label: "prima parte | a doua parte | ultima parte | ...", started_at: time | null }] | null`**. Optional alongside the flat `chair[]` so consumers that only need the unordered list can ignore segments.

**X3-4. Narrator activities encode protest, banner-display, and walkout events, not just `_(Aplauze.)_`.** Sample `2023-05-08_MO-PII-57-2023.md` line 54 (`_(Membrii Grupului parlamentar AUR au pancarte ...)_`), line 60 (`_(Domnul deputat ... merge cu o pancartă lângă loja miniștrilor.)_`), plus mic-cut and floor-request narrators throughout the corpus. Verbatim text is preserved by the existing `narrator` activity, so search already works — but a sub-classifier would let UIs facet "all walkout events in legislature IX".
**Defer to v1.4** as an optional `narrator.kind` enum (`applause | murmur | silence | protest | walkout | mic_cut | request_floor | speaker_arrives | speaker_leaves | anthem | other`). Lower priority: the verbatim string already serves search; this is a faceting nicety, not load-bearing for any current consumer.

### Summary of 1.2.0 → 1.3.0 deltas

| # | Delta | Why |
|---|---|---|
| 1 | New `document_type: "question_register"` | End-of-session unanswered-question lists are structurally distinct from stenograms; ≥20 corpus files |
| 2 | `agenda_items[].category` gains `government_confidence` (with `confidence_type` sub-flag) | Investiture / cenzură / engagement-of-responsibility are constitutional events not covered by `motion` |
| 3 | `agenda_items[].category` gains `government_hour` | "Ora Guvernului" — group-requested ministerial Q&A; 63+ corpus files |
| 4 | `agenda_items[].category` gains `oath_taking` | Distinct from `appointment` (often a different document; different procedural elements) |
| 5 | `agenda_items[].category` gains `mandate_validation` | Procedural complement of `seat_vacancy`; 199+ corpus hits; load-bearing for the person registry |
| 6 | `vote.motion_type` gains `system_check` | Pre-vote hardware tests pollute per-MP voting records if treated as substantive votes |
| 7 | `vote.voting_method` gains `telephone_roll_call` | Pandemic-era hybrid voting; distinct accountability proxy from in-chamber `nominal` |
| 8 | `session.special_procedure` gains `sedinta_solemna` and `deschiderea_legislaturii` | Annual National Day observances and once-per-legislature opening sessions are structurally unique |
| 9 | `Reference.eu_doc.series` gains `JOIN` | Co-issued Commission + High Representative documents; recurrent across 13-year corpus |
| 10 | Committee `agenda[].output_type` gains `studiu` and `proiect_de_opinie` | Dominant outputs of preparatory review; `studiu` was getting miscategorized as `amânare` |
| 11 | Committee `meeting.joint_with[]` items: `string` → `{ name, chamber }` | Cross-chamber joint committee meetings exist; flat string loses the chamber discriminator |
| 12 | Committee `committee.kind: "permanent | special | inquiry | special_joint | inquiry_joint"` field added | Time-limited single-subject committees have a distinct lifecycle from permanent ones |
| 13 | `speech.delivery_mode` promoted from deferred to implemented (with explicit enum) | Universal in 2020-2022 corpus; multi-year structural signal would otherwise be lost |
| 14 | `session.chair_segments[]` promoted from reserved to implemented | Mid-session chair swaps are routine; flat `chair[]` is too coarse for accountability |

All 14 are additive. Item 11 is the only type-shape change — the migration is mechanical (wrap each existing `joint_with` string in `{ name, chamber: derived }`). Combined `agenda_items[].category` enum after v1.3.0 reaches 22 values; combined `document_type` reaches 6 values. The discriminator pattern continues to scale; resist creating new top-level types for things that fit cleanly into an existing body shape.

The audit also confirmed that several v1.2.0 fields are well-supported in this period: `b` bill prefix (multiple 2022/2023 hits), `not_voting` vote count (every modern electronic-voting tally), `subsidiarity_check` / `delegation_membership` / `parliamentary_declaration` / `seat_vacancy` / `party_membership_change` agenda categories all appear in the sample exactly as v1.2.0 anticipated. One single-sample finding (presidential authorization requests under article 92/93 — `2025-10-13_MO-PII-117-2025.md` items 14 & 19) was flagged but not promoted to a delta; revisit if it recurs in further audits.

## Schema revisions from second 10-year audit (n=50, 2016-05 → 2026-05)

After 1.3.0 stabilised, we ran a fourth audit pass: **50 random documents from the most recent 10 years**. The corpus has gaps before 2018, so the sample landed mostly in 2018-2026 with a heavier tilt towards Senate ordinary-session stenograms and committee syntheses than the v1.3.0 pass. The audit surfaced a band of agenda-categories the prior passes had missed — chamber-internal officer elections, committee membership reorganisation, the EU Protocol-1 consultation procedure (a sibling of v1.2.0's Protocol-2 `subsidiarity_check`), question-and-interpellation block items, and the constitutional-deadline-extension procedure — plus three structural gaps: pandemic-era remote electronic voting (a distinct `voting_method`), session-level outcome (sessions that close prematurely for lack of quorum), and joint-committee report output (the most common committee output not yet enumerated).

Bumping `schema_version` to **1.4.0** (additive only; one previously-singular committee field gets pluralised in line with the v1.1.0 pattern). Findings recorded in the same `P4-N` / `C4-N` / `X4-N` format.

### Findings — plenary stenograms

**P4-1. Chamber-internal officer elections / dismissals are a recurring agenda category absent from v1.3.0.** Sample `2022-02-09_MO-PII-9-2022.md` items 5, 9, 10, 11: `Alegerea membrilor Biroului permanent al Senatului (4 vicepreședinți, 4 secretari și 4 chestori)`, `Revocarea secretarului general al Senatului`, `Numirea secretarului general al Senatului`, `Numirea unui secretar general adjunct al Senatului`. 43+ corpus hits. v1.3.0 routes these to `appointment` — but `appointment` is documented as "to extra-parliamentary bodies (CSM, CNI, CCR, etc.)", which conflates two categorically different events: external constitutional appointments and chamber-internal staffing. Conflation breaks the natural query "show me when the Senate Bureau changed".
**Add `agenda_items[].category: "chamber_officer"`** for chamber-internal positions (Biroul Permanent, vicepreședinți, secretari, chestori, secretar general, secretar general adjunct). `appointment` retains the external-bodies semantics; `delegation_membership` retains the international-assemblies semantics; `chamber_officer` slots between them for the third pattern.

**P4-2. Internal committee membership reorganisation is a high-volume agenda category.** Sample `2018-07-17_MO-PII-118-2018.md` item 3 ("Aprobarea unor modificări în componența nominală a comisiilor permanente ale Senatului"), `2025-03-18_MO-PII-27-2025.md` item 1 ("Aprobarea unor modificări în componența numerică a Comisiei pentru cercetarea abuzurilor ... și a unor modificări în componența nominală a unor comisii permanente"). 75+ corpus hits. These differ from `chamber_officer` (which is about chamber-wide officers) and from `appointment` / `delegation_membership` (external bodies / international assemblies); they're the routine reshuffling of who sits on which standing committee.
**Add `agenda_items[].category: "committee_membership"`**. Distinct from `chamber_officer` because the position is *committee membership*, not chamber-wide officership; queryable separately ("when did the Health Committee composition last change?") for the future person-registry's mandate-window tracking.

**P4-3. The EU Protocol-1 consultation procedure is structurally distinct from v1.2.0's `subsidiarity_check` (Protocol 2).** Sample `2019-11-05_MO-PII-124-2019.md` item 3 ("Dezbaterea și adoptarea proiectelor de hotărâre privind consultarea parlamentelor naționale conform Protocolului nr. 1 din Tratatul de la Lisabona"), `2024-12-19_MO-PII-141-2024.md` items 22-23 ("Proiectul de hotărâre privind adoptarea opiniei referitoare la Comunicarea Comisiei ... — COM(2024) 91"). 70+ direct corpus hits for "Protocolului nr. 1"; 253+ for the "adoptarea opiniei" pattern. Protocol 1 governs *information-and-consultation* on EU Commission communications, distinct from Protocol 2's *subsidiarity-and-proportionality check* on legislative proposals. Same session frequently hosts both as separate agenda items (2019-11-05 has Protocol 1 at item 3 and Protocol 2 at item 5). Forcing both into `subsidiarity_check` collapses a constitutionally relevant distinction.
**Add `agenda_items[].category: "eu_consultation"`**. Carries `Reference.eu_doc[]` references like `subsidiarity_check`; the discriminator is the constitutional procedure invoked.

**P4-4. Senate sessions routinely list "Întrebări, interpelări" as a top-level agenda block.** Sample `2021-10-18_MO-PII-147-2021.md` item 1 (`Întrebări, interpelări`), item 2 (`Declarații politice`). 110+ corpus hits. The interpellation contents already extract into the v1.0.0 sibling `interpellations[]` array — but the *agenda-level* wrapper has no representation. v1.1.0's `political_declarations` covers the analogous block-of-MP-declarations pattern; the question/interpellation block is structurally parallel and should sit alongside it in the category enum, with its child interpellation records continuing to live in the `interpellations[]` sibling.
**Add `agenda_items[].category: "questions_interpellations"`**. Behaves like `political_declarations`: the agenda item is a wrapper; the substance lives in a sibling block. Keeps the agenda-ordinal/source-span machinery aligned with the rest of the schema while preserving the existing `interpellations[]` shape.

**P4-5. Constitutional-deadline extension to "particularly complex" laws is a recurring procedural action.** Sample `2020-12-07_MO-PII-128-2020.md` item 6 (`Aprobarea solicitării Comisiei juridice ... cu privire la încadrarea în categoria legilor de complexitate deosebită și, în consecință, prelungirea termenului constituțional de dezbatere și vot final de la 45 la 60 de zile`). 47+ corpus hits. Romanian parliamentary procedure imposes a default 45-day deliberation window after which `tacit_adoption` kicks in; for "complex" legislation the chamber can vote to extend to 60 days. v1.3.0 would force this under generic `procedural`, but the action is constitutionally consequential — it defers the tacit-adoption clock — and is queryable signal ("which bills had their deadline extended?").
**Add `agenda_items[].category: "deadline_extension"`**. Carries the bill `Reference[]` whose deadline is being extended (often a list).

**P4-6. The opening of an ordinary/extraordinary session within a legislature is a recurring `special_procedure`.** Sample `2022-02-09_MO-PII-9-2022.md` item 1 (`Deschiderea sesiunii ordinare a Senatului`). 17+ corpus hits. The v1.3.0 enum has `deschiderea_legislaturii` for the once-per-legislature inaugural session; ordinary/extraordinary sessions open twice a year and are procedurally distinct (no Validation Commission, no senior-president presider, but specific protocol elements like the chamber's national-anthem opening). Neither `deschiderea_legislaturii` nor v1.2.0's `sedinta_solemna` / `declaratie_solemna` covers this case cleanly.
**Extend `session.special_procedure`** to add `"deschiderea_sesiunii"`. Final enum value list: `buget_de_stat | declaratie_solemna | mesaj_prezidential | sedinta_solemna | deschiderea_legislaturii | deschiderea_sesiunii | null`.

**P4-7. Sessions that close prematurely for lack of quorum need a session-level outcome field.** Sample `2020-02-26_MO-PII-17-2020.md` is a joint session that opened at 16:05, found 188 of 465 parliamentarians present, and closed at 16:09 with a one-line "lipsă de cvorum" reason. The `session.quorum_met: bool` field captures the boolean fact, but consumers querying "which sessions failed to reach quorum" must walk the body to detect the absence of any agenda items — fragile. 12+ direct corpus hits for the suspended-no-quorum pattern (39+ if counting all "lipsă de cvorum" mentions including mid-session quorum losses).
**Add `session.outcome: "completed | suspended_no_quorum | suspended_other | adjourned"`** field. Default `completed` for typical sessions. `suspended_no_quorum` for session aborted at the start; `suspended_other` for adjournments due to other procedural blockers; `adjourned` for sessions deliberately ended early. Distinct from agenda-item `outcome`: the former is per-debate, this is per-session.

**P4-8. "Vot electronic la distanță" is a sixth voting method, distinct from in-room electronic voting.** Sample `2020-12-24_MO-PII-129-2020.md` item 17 (`adoptat prin vot electronic la distanță` repeated for 5+ bills in a final-vote batch); `2021-12-24_MO-PII-190-2021.md` and similar throughout 2020-2022. 53+ corpus files. Pandemic-era hybrid voting introduced *remote* electronic voting (deputies vote from outside the chamber via the parliamentary app) — distinct accountability proxy from in-room electronic voting (deputy is physically present at the desk). v1.3.0's `voting_method: "electronic"` conflates both. The existing v1.3.0 `telephone_roll_call` value precedented adding pandemic-era voting methods as distinct enum values.
**Extend `vote.voting_method`** to add `"electronic_remote"`. Final enum: `electronic | electronic_remote | show_of_hands | secret_ballot | nominal | telephone_roll_call | by_acclamation`.

**P4-9. Bills carry a procedure annotation: ordinary vs urgency procedure.** Sample `2024-06-17_MO-PII-18c-2024.md` items repeatedly carry "; procedură de urgență" in the agenda title (`PL-x 192/2024, procedură de urgență, fond comun cu Comisia pentru buget, finanțe și bănci`); the same pattern shows in plenary across the corpus. 1156+ corpus hits — overwhelming. Different from `bill.category` (ordinară/organică/constituțională, which encodes legislative-type) — `procedură de urgență` is a *procedural status* affecting deliberation timeline (shortened windows, expedited committee review). Querying "all urgency-procedure bills" is a real research need; the field is currently unrepresented.
**Add `Reference.bill.procedure: "ordinary | urgency" | null`**. Defaults to null when not stated; populated regex-deterministically from the `procedură de urgență` annotation.

### Findings — committee syntheses

**C4-1. `raport_comun` (joint report) is the most common committee output not in v1.3.0's enum.** Sample `2024-06-17_MO-PII-18c-2024.md` line 147 (tabular column: `Raport comun cu Comisia pentru muncă și protecție socială`); occurrences span 2013 → 2026. 1022+ corpus mentions of `raport comun`; 336+ in committee tabular form. v1.3.0 has `raport`, `raport_preliminar`, `raport_suplimentar`, `aviz`, `studiu`, `proiect_de_opinie`, `amânare` — none of which capture the joint-authorship pattern that `committee_role: "fond_comun"` (v1.1.0 C-6) naturally produces. The `co_committees[]` slot (v1.1.0 C-5) already carries the co-authoring committee list; what's missing is the output-type discriminator that says "this is a *joint* report, not a *solo* report".
**Extend `agenda[].output_type`** to add `"raport_comun"` and `"raport_comun_suplimentar"`. Final enum: `raport | raport_preliminar | raport_suplimentar | raport_comun | raport_comun_suplimentar | aviz | studiu | proiect_de_opinie | amânare`. Naturally pairs with `committee_role: "fond_comun"` and a populated `co_committees[]`.

**C4-2. Preliminary-report addressee is plural, not singular.** Sample `2024-06-17_MO-PII-18c-2024.md` tabular rows: `Aviz pentru Comisia pentru politică economică, reformă și privatizare și Comisia pentru buget, finanțe și bănci`; `Raport preliminar pentru Comisia X și Comisia Y`. 10+ corpus files with the multi-addressee pattern; per agenda item it's frequent. v1.1.0's `for_committee: string | null` is too narrow — preliminary reports and avize regularly target multiple downstream committees at once.
**Promote `agenda[].for_committee` → `for_committees: string[]`**. Mechanical migration: wrap each existing string in a 1-element array. Same pluralisation pattern as v1.1.0's `co_committee` → `co_committees` and v1.3.0's `joint_with[]` shape change.

**C4-3. Confirmation hearings for executive nominees are a recurring meeting purpose.** Sample `2021-06-24_MO-PII-22c-2021.md` lines 160-172: a five-committee joint hearing of candidates for the Competition Council presidency / vice-presidency, where each committee votes `aviz favorabil/nefavorabil` on the candidacy. 84+ corpus hits for the audition pattern. v1.1.0's `meeting.purpose: "documentare_consultare | dezbatere_decizie | aprobare_raport | null"` doesn't have a value for the audition genre; rolling it under `dezbatere_decizie` loses the queryable signal "which committees vetted nominee X".
**Extend `meeting.purpose`** to add `"audiere_candidați"`. Final enum: `documentare_consultare | dezbatere_decizie | aprobare_raport | audiere_candidați | null`. Output-type for the agenda items remains `aviz` (the committee produces an aviz on the candidacy); `purpose` records what the meeting was *for*. The candidate identity itself surfaces through the `Speaker` shape inside `guests[]` — no new field needed there.

### Summary of 1.3.0 → 1.4.0 deltas

| # | Delta | Why |
|---|---|---|
| 1 | `agenda_items[].category` gains `chamber_officer` | Bureau Permanent / Secretary General / Quaestor elections; was conflated with `appointment` |
| 2 | `agenda_items[].category` gains `committee_membership` | High-volume internal committee membership reorg; needed for mandate-window tracking |
| 3 | `agenda_items[].category` gains `eu_consultation` | EU Protocol-1 consultation, structurally distinct from Protocol-2 `subsidiarity_check` |
| 4 | `agenda_items[].category` gains `questions_interpellations` | Senate top-level agenda block wrapping the existing `interpellations[]` sibling array |
| 5 | `agenda_items[].category` gains `deadline_extension` | 45→60 day deliberation extension defers tacit-adoption clock; queryable signal |
| 6 | `session.special_procedure` gains `deschiderea_sesiunii` | Twice-yearly ordinary/extraordinary session opening, distinct from `deschiderea_legislaturii` |
| 7 | New `session.outcome` field | Captures sessions that close prematurely (no quorum, adjourned); 12+ corpus hits |
| 8 | `vote.voting_method` gains `electronic_remote` | Pandemic-era remote voting; distinct accountability proxy from in-room `electronic` |
| 9 | New `Reference.bill.procedure` field | `procedură de urgență` annotation; 1156+ corpus hits, currently unrepresented |
| 10 | Committee `agenda[].output_type` gains `raport_comun` and `raport_comun_suplimentar` | Most common committee output not in v1.3.0 enum; 1022+ corpus mentions |
| 11 | Committee `agenda[].for_committee` → `for_committees: string[]` | Multi-committee preliminary report addressees |
| 12 | Committee `meeting.purpose` gains `audiere_candidați` | Confirmation hearings for executive nominees; 84+ corpus hits |

All 12 are additive. Item 11 is the only type-shape change — mechanical migration (wrap each existing `for_committee` string in a 1-element array, same pattern as v1.1.0's `co_committee` and v1.3.0's `joint_with[]`). Combined `agenda_items[].category` enum after v1.4.0 reaches 27 values; combined `session.special_procedure` reaches 6 values; combined committee `output_type` reaches 9 values. The discriminator pattern continues to scale.

The audit also confirmed that several v1.3.0 fields are well-supported across the broader 10-year window: `delivery_mode` annotations (887+ corpus hits — the v1.3.0 X3-2 promotion was timely), `chair_segments[]` (mid-session swaps remain routine), `not_voting` electronic-vote count (universal in modern tallies), `government_confidence` / `government_hour` / `oath_taking` / `mandate_validation` agenda categories all appear in the 50-doc sample exactly as v1.3.0 anticipated. One single-sample finding (the inconclusive committee vote in `2020-02-28_MO-PII-4c-2020.md` where neither `aviz favorabil` nor `aviz nefavorabil` reached majority) was flagged but not promoted to a delta; only one corpus hit, falls under existing `vote_summary.outcome: "deferred"` in practice (the committee re-runs the vote next week). Revisit if it recurs.

## Schema revisions for the extract pipeline scaffolding (1.4.0 → 1.5.0)

The v1.0.0 → v1.4.0 bumps were corpus-driven: each audit pass surfaced agenda categories, vote types, or committee shapes the prior version had missed. The v1.5.0 bump is **engineering-driven**: it locks the envelope additions the `extract` subcommand needs to operate, plus the diagnostic block the discovery loop reads to find the next round of corpus-driven deltas. No body shape changes — the per-type bodies from v1.4.0 are unchanged.

### E5-1. Per-component `extractor_versions` block

The v1.0.0 example carried three placeholder keys (`regex`, `speaker_parser`, `topic_classifier`). Real implementation has more moving parts: shared helpers (`boilerplate`, `references`, `speakers`, `coverage`) plus one key per per-type extractor (`question_register`, `plenary_stenogram`, ...). Each component's version travels with the sidecars whose body content depends on it, so a bump to (say) `references` triggers re-extraction of only the docs whose body shape contains references — not the whole corpus.

**Schema shape.** `extractor_versions: { [a-z_]+: "x.y.z" | null }`, open-ended dict. Null is allowed for components not yet wired in (matches the v1.0.0 `topic_classifier: null` precedent). The schema validator accepts any subset; the extract pipeline always emits at least one key.

### E5-2. New `coverage` envelope block

The schema-discovery loop is **extract → measure unaccounted spans → inspect → improve extractor → re-extract**. `coverage` surfaces the unaccounted spans without forcing the developer to hand-diff sidecar content against the source MD.

```json
"coverage": {
  "body_chars": 45230,
  "claimed_chars": 44890,
  "claimed_pct": 0.9925,
  "gaps": [
    { "chars": [12340, 12390], "lines": [421, 423], "preview": "first ~120 chars of unclaimed text…" }
  ],
  "claimed_by_policy": [
    { "chars": [0, 87], "lines": [1, 4], "reason": "shared_boilerplate.partea_header" }
  ]
}
```

Two ways a span gets claimed:

1. **Real extraction** — a record (`question`, `activity`, `vote`, ...) emits a `source_span` covering the span.
2. **By-policy skip** — boilerplate the extractor *intentionally* ignores (the `# CAMERA DEPUTAȚILOR` heading, the `Joi, 11 iulie 2013` date line) is recorded in `coverage.claimed_by_policy[]` with a reason. This is the explicit ledger that prevents the gap report from drowning in known-noise.

Anything > 20 chars that isn't claimed by either path becomes a gap. **Diagnostic-only — never gates writes.** `extract --coverage-below MARGIN` (analogous to `classify --outliers`) emits docs whose `claimed_pct < MARGIN`, the discovery-loop entry point.

### E5-3. Source span coordinate system locked

All `lines` and `chars` arrays are **1-indexed within the body text** (the bytes after the YAML frontmatter close). `content_sha` is sha256 of the body bytes truncated to 12 hex chars — matches the existing PDF-sha convention from the fetch pipeline. Frontmatter spans are not addressable: frontmatter content is already structured into `metadata`, so no extracted record should reference it. Locking this up front removes the ambiguity that would otherwise drift extractor-by-extractor.

### E5-4. By-policy boilerplate ledger separated from body

By-policy claims live in `coverage.claimed_by_policy[]`, **not** in any body shape. The per-type body definitions remain exactly as v1.4.0 specified them. This keeps coverage as a purely-envelope concern and lets boilerplate detection evolve (new patterns added, `boilerplate` version bumped) without touching any body shape — and without forcing each per-type extractor to reinvent boilerplate-emit machinery in its body output.

The shared boilerplate detector (`monitorul_ii.extraction.boilerplate.claim_shared_boilerplate`) starts empty and grows from gap-report iterations; per-extractor boilerplate emitters add type-specific patterns (e.g., the question_register-only `LISTA ÎNTREBĂRILOR ADRESATE…` heading) to the same ledger.

### Summary of 1.4.0 → 1.5.0 deltas

| # | Delta | Why |
|---|---|---|
| 1 | `envelope.extractor_versions` typed as open-ended `{ [a-z_]+: "x.y.z" \| null }` dict, replacing the 3-key placeholder example | Per-component versioning enables selective re-extraction when one helper bumps |
| 2 | New `envelope.coverage` block (`body_chars`, `claimed_chars`, `claimed_pct`, `gaps[]`, `claimed_by_policy[]`) | Diagnostic for the extract→audit→improve loop; surfaces unaccounted spans for the next extractor version to cover |
| 3 | Source span coordinate system locked: body-only, 1-indexed lines, sha256-truncated-12 `content_sha` | Removes ambiguity that would otherwise drift extractor-by-extractor |
| 4 | By-policy boilerplate ledger lives in `coverage.claimed_by_policy[]`, not in any body shape | Keeps body shapes clean per their per-type v1.4.0 definitions |

All four are envelope-level. Body shapes from v1.4.0 are unchanged. The bump is non-breaking for any tool that reads only fields documented in v1.4.0; existing v1.4.0 sidecars (none yet on disk — `extract` is the first writer) would need a one-time mechanical upgrade to add the new envelope keys.

## Consolidated schema reference

Common envelope every document carries:

```json
{
  "schema_version": "1.5.0",
  "document_id": "mo://2026/PII/48",
  "content_sha": "a3f9c1d2e4b8",
  "document_type": "plenary_stenogram | plenary_joint_session | committee_synthesis | report_facsimile | question_register | other",
  "metadata": {
    "issue": "48",
    "year": 2026,
    "part": "II",
    "published": "2026-04-29",
    "chamber": "Camera Deputaților | Senatul | joint",
    "session": "SESIUNEA I ORDINARĂ – APRILIE 2026",
    "session_type": "ordinary | extraordinary | null",
    "session_date": "2026-04-14",
    "legislature": "X"
  },
  "raw_markdown_path": "pdfs/2026-04-29_MO-PII-48-2026.md",
  "raw_pdf_path": "pdfs/2026-04-29_MO-PII-48-2026.pdf",
  "extraction": {
    "extractor": "hybrid@1",
    "extracted_at": "2026-05-04T08:30:00Z",
    "extractor_versions": {                                         // 1.5.0: per-component dict, open-ended
      "boilerplate": "0.1.0",
      "coverage": "0.1.0",
      "references": "0.1.0",
      "speakers": "0.1.0",
      "question_register": "0.1.0",
      "topic_classifier": null
    },
    "confidence": 0.94
  },
  "coverage": {                                                     // 1.5.0: added
    "body_chars": 45230,
    "claimed_chars": 44890,
    "claimed_pct": 0.9925,
    "gaps": [
      { "chars": [12340, 12390], "lines": [421, 423], "preview": "..." }
    ],
    "claimed_by_policy": [
      { "chars": [0, 87], "lines": [1, 4], "reason": "shared_boilerplate.partea_header" }
    ]
  },
  "body": { "...one of the five shapes below..." }
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
// type = bill (1.4.0: procedure field added)
{ "type": "bill", "prefix": "PL-x | Pl-x | L | b", "number": "257", "year": 2019,
  "secondary_year": null, "chamber_of_origin": "camera",
  "category": "ordinară", "procedure": "ordinary | urgency | null",
  "raw": "PL-x 257/2019", "char_offsets": [120, 134] }

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

// type = eu_doc (1.3.0: series enum gains "JOIN")
{ "type": "eu_doc", "series": "COM | JOCE | REG | DIR | JOIN", "year": 2013, "number": "146",
  "subseries": "final", "subject": null,
  "raw": "COM(2013) 146 final", "char_offsets": [0, 19] }

// type = treaty
{ "type": "treaty", "name": "Tratatul de la Lisabona", "signed_date": null,
  "raw": "...", "char_offsets": [0, 0] }

// type = chamber_resolution (1.1.0; prefix gains "HP" in 1.2.0)
// HP = Hotărârea Parlamentului României (joint resolution; chamber: "joint")
{ "type": "chamber_resolution", "prefix": "PHCD | PHS | PHCDS | HP", "number": "41", "year": 2025,
  "chamber": "Camera Deputaților | Senatul | joint",
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
    "chair_segments": [                                       // 1.3.0: promoted from reserved
      { "chair": Speaker, "secretaries": [Speaker, "..."],
        "segment_label": "prima parte | a doua parte | ultima parte | ...",
        "started_at": "16:01" }
    ],
    "secretaries": [Speaker, "..."],
    "attendance": { "registered": 185, "total_seats": 330 },
    "quorum_met": true,
    "opened_at": "16:01",
    "closed_at": null,
    "format": "in_person | online | mixed",
    "outcome": "completed | suspended_no_quorum | suspended_other | adjourned",  // 1.4.0: added
    "special_procedure": "buget_de_stat | declaratie_solemna | mesaj_prezidential | sedinta_solemna | deschiderea_legislaturii | deschiderea_sesiunii | null"  // 1.4.0: enum extended
  },
  "agenda_items": [
    {
      "ordinal": 4,
      "title": "...",
      "primary_references": [Reference, "..."],
      "category": "bill_debate | political_declarations | commemorative | final_vote_batch | procedural | tacit_adoption | appointment | motion | committee_report_presentation | legislative_transmission | withdrawal | notification | regulation_amendment | party_membership_change | subsidiarity_check | foreign_address | seat_vacancy | parliamentary_declaration | delegation_membership | government_confidence | government_hour | oath_taking | mandate_validation | chamber_officer | committee_membership | eu_consultation | questions_interpellations | deadline_extension",  // 1.4.0: 5 added
      "confidence_type": "investitură | cenzură | angajare_răspundere | demitere | null",   // 1.3.0: when category=government_confidence
      "requested_by_group": null,                              // 1.3.0: when category=government_hour
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
          "delivery_mode": "tribune | from_floor | written | online | from_balcony | null",  // 1.3.0: promoted from deferred
          "text": "...",
          "references_mentioned": [Reference, "..."],
          "source_span": SourceSpan,
          "extraction": PerSectionExtraction
        },
        {
          "type": "vote",
          "motion_text": "...",
          "motion_type": "procedural | amendment | final | item_adoption | agenda_approval | urgency_procedure | report_approval | system_check",  // 1.3.0: system_check added
          "voting_method": "electronic | electronic_remote | show_of_hands | secret_ballot | nominal | telephone_roll_call | by_acclamation",  // 1.4.0: electronic_remote added
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
      "genre": "întrebare | interpelare",        // 1.2.0: added — procedurally distinct objects
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
      "kind": "permanent | special | inquiry | special_joint | inquiry_joint",  // 1.3.0: added
      "chair": Speaker,
      "meetings": [
        {
          "dates": ["2013-04-08"],                         // 1.1.0: was singular
          "joint_with": [                                  // 1.3.0: items: string → { name, chamber }
            { "name": "Comisia pentru buget, finanțe și bănci", "chamber": "camera" }
          ],
          "presider": Speaker,
          "format": "in_person | online | mixed",          // 1.1.0: added
          "purpose": "documentare_consultare | dezbatere_decizie | aprobare_raport | audiere_candidați | null",  // 1.4.0: audiere_candidați added
          "time_windows": [{ "start": "08:30", "end": "12:00" },
                           { "start": "13:00", "end": "18:00" }],  // 1.1.0: was started_at
          "roster": [                                      // 1.1.0: replaces attendees+absentees+substitutions
            { "speaker": Speaker,
              "mode": "physical | online | absent | substituted",
              "intra_committee_role": "președinte | vicepreședinte | secretar | membru",  // 1.2.0: added
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
              "output_type": "raport | raport_preliminar | raport_suplimentar | raport_comun | raport_comun_suplimentar | aviz | studiu | proiect_de_opinie | amânare",  // 1.4.0: raport_comun, raport_comun_suplimentar added
              "for_committees": ["Comisia pentru muncă și protecție socială"],  // 1.4.0: pluralised — populated when output_type ∈ {raport_preliminar, aviz}
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
    "outcome": "completed | suspended_no_quorum | suspended_other | adjourned",  // 1.4.0: added
    "special_procedure": "buget_de_stat | declaratie_solemna | mesaj_prezidential | sedinta_solemna | deschiderea_legislaturii | deschiderea_sesiunii | null"  // 1.4.0: aligned with plenary enum
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

### body for `report_facsimile`

Added in 1.2.0. For `R`-suffix issues — external reports submitted to Parliament and reproduced verbatim ("reprodus în facsimil"). Typically annual activity reports from constitutional bodies (CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului) received in joint session but not substantively debated. The body intentionally carries minimal structure: report metadata + a heading outline. The full text remains in the sidecar markdown.

```json
{
  "report": {
    "title": "Raportul Consiliului Suprem de Apărare a Țării privind activitatea desfășurată în anul 2012",
    "issuing_body": "Consiliul Suprem de Apărare a Țării",
    "issuing_body_normalized": "CSAT",        // backfillable, like Speaker.person_id
    "reporting_period": { "start": "2012-01-01", "end": "2012-12-31" },
    "received_at": {
      "session_kind": "joint | camera | senat",
      "session_date": "2013-12-04",
      "received_in_document": "mo://2013/PII/X" // back-link to the stenogram that recorded reception, if any
    }
  },
  "headings": [
    { "level": 1, "text": "CONTEXT", "line": 41 },
    { "level": 1, "text": "CAPITOLUL I. Cadrul organizatoric", "line": 43 },
    { "level": 1, "text": "CAPITOLUL II. Coordonarea activității...", "line": 45 }
  ],
  "raw_markdown_excerpt": "first ~500 chars for snippet generation",
  "extraction": PerSectionExtraction
}
```

### body for `question_register`

Added in 1.3.0. End-of-session lists of unanswered written questions, published as standalone MO Partea II issues. Headed `LISTA ÎNTREBĂRILOR ADRESATE DE CĂTRE DEPUTAȚI/SENATORI ... LA CARE NU S-A PRIMIT RĂSPUNS`. No agenda, no votes, no debate — a flat catalogue grouped by addressee. Detection rule: header contains `LISTA ÎNTREBĂRILOR ADRESATE` AND body lacks `(STENOGRAMA)` marker.

```json
{
  "session_label": "SESIUNEA I ORDINARĂ – FEBRUARIE 2026",
  "chamber": "Camera Deputaților | Senatul",
  "questions": [
    {
      "ordinal": 1,
      "addressee": {
        "ministry": "Ministerul Educației și Cercetării",
        "ministry_normalized": null,
        "name": "Daniel-David",
        "role": "ministrul educației și cercetării"
      },
      "questioner": Speaker,
      "registration_number": "1.172B",        // verbatim from the list
      "registration_date": "2025-12-12",      // when known
      "topic": "...",
      "question_text": null,                  // questions are typically filed in writing; the published list rarely includes the body
      "source_span": SourceSpan,
      "extraction": PerSectionExtraction
    }
  ]
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
| `report.issuing_body_normalized` | `institutional_bodies` registry — CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului, etc. | After enough `report_facsimile` documents to enumerate the reporting institutions (small set, ~20) |
| `report.received_at.received_in_document` | Cross-document linker pass | After plenary stenograms are extracted; many reports are received in a joint session whose stenogram is its own document |

## Build order

The list below is the *conceptual priority by corpus impact* — `plenary_stenogram` carries the bulk of queryable signal. The actual development sequence (revised 2026-05-04) inserts a **Step 1.5: extract subcommand scaffolding** (envelope construction, coverage computation, JSON Schema validation, sidecar I/O, version-aware idempotency, CLI dispatch by classifier) and inverts the per-type order to ship `question_register` first as a shakeout for the scaffolding. Rationale: question_register's body is the schema's simplest (one flat list of questions per minister-addressee), its corpus is its smallest (43 docs), and bugs in the shared scaffolding cost less to find on 43 docs than on the 1667-doc plenary_stenogram cohort. Once question_register sidecars stabilise, `plenary_stenogram` becomes the main course.

1. **Type detector.** Cheap regex on issue suffix + body markers; classify all converted MDs into the six buckets (`plenary_stenogram | plenary_joint_session | committee_synthesis | report_facsimile | question_register | other`). Detect joint sessions via `ȘEDINȚE COMUNE ALE CAMEREI DEPUTAȚILOR ȘI SENATULUI` header. Detect `report_facsimile` via `R` issue suffix (`3/R/2014`) and `(RAPOARTE DE ACTIVITATE)` body marker. Detect `committee_synthesis` via `c` suffix (`13c/2013`). Detect `question_register` via `LISTA ÎNTREBĂRILOR ADRESATE` header AND absence of `(STENOGRAMA)` marker. Sanity check the distribution against expected ratios.
2. **`plenary_stenogram` extractor.** Covers the bulk of the queryable corpus. Speaker parser with `delivery_mode` annotation, agenda enumeration with the 27-value `category` enum (1.4.0) including v1.3.0's `government_confidence` / `government_hour` / `oath_taking` / `mandate_validation` and v1.4.0's `chamber_officer` / `committee_membership` / `eu_consultation` / `questions_interpellations` / `deadline_extension` values, reference regex pack including PHCD/HP, motion types, the `b` bill prefix, the `JOIN` EU-doc series, and the v1.4.0 `bill.procedure` flag for `procedură de urgență`, vote detector with deferred-handling, `not_voting` count, the `system_check` motion-type filter, and the v1.4.0 `electronic_remote` voting method, interpellation block parser with `genre` discrimination, session shape with `chair_segments[]` for mid-session swaps, the extended `special_procedure` enum (including v1.4.0's `deschiderea_sesiunii`), and the v1.4.0 `outcome` field for premature-closure detection. Topics primary populated from agenda titles via regex.
3. **`plenary_joint_session` extractor.** Reuses most of the plenary extractor; key additions are dual-chair detection, `chambers_present[]` population, and parallel-reference parsing in agenda titles (e.g., `L146/2026; PL-x 184/2026`).
4. **`committee_synthesis` extractor.** Meeting-as-atom with `dates[]` and `time_windows[]`, unified `roster[]` with `mode` and `intra_committee_role` per member, `committee_role` and `output_type` per agenda entry (with v1.3.0's `studiu` / `proiect_de_opinie` and v1.4.0's `raport_comun` / `raport_comun_suplimentar` outputs), `for_committees[]` for multi-recipient preliminary reports (v1.4.0 pluralisation), `committee.kind` discriminator for special / inquiry committees, structured `joint_with[]` with chamber discriminator, narrative vote summaries, and v1.4.0's `meeting.purpose: "audiere_candidați"` for confirmation hearings. Must handle both narrative and tabular sub-formats.
5. **`report_facsimile` extractor.** Minimal — extract issuing body from header, reporting period from title (regex on `anul YYYY`), heading outline from markdown H1/H2. Body remains in the sidecar markdown. Issuing body normalization deferred to the institutional bodies registry.
6. **`question_register` extractor.** Flat catalogue parser — group questions by minister-addressee header, capture per-question registration number, date, questioner, topic. `ministry_normalized` deferred to the ministries registry alongside `interpellation.addressed_to_normalized`.
7. **`other` fallback.** Write the audit-trail JSON for everything that doesn't match. Confirm zero documents fail to classify.
8. **(Later)** Person registry → backfill `person_id` across all `Speaker` instances.
9. **(Later)** Legislation registry → backfill `*_id` on `Reference` instances.
10. **(Later)** Ministry registry → backfill `addressed_to_normalized` on interpellations *and* `question_register.questions[].addressee.ministry_normalized`.
11. **(Later)** Institutional bodies registry → backfill `report.issuing_body_normalized` (small enum, ~20 entries: CSAT, SRI, SIE, BNR, ICR, Avocatul Poporului, etc.).
12. **(Later)** LLM topic classifier → populate `topics.secondary`.
13. **(Later)** Cross-document linker → populate `vote.defers_to` / `resolves` between deferral activities and final-vote items, plus `report.received_at.received_in_document` for facsimile reports.
14. **(Later)** Elasticsearch ingest. Denormalize speeches, interpellations, votes into ES indices for fielded search and embedding. The hierarchical JSON is the source; ES ingest produces flat row projections natively.
