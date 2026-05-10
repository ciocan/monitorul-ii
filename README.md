# monitorul-ii

Scrape [Monitorul Oficial al României](https://monitoruloficial.ro/e-monitor/) Partea a II-a (and other parts), save the PDFs locally, and convert them to extraction-friendly markdown.

## Install

```sh
uv sync
```

## Usage

Six core subcommands: `fetch` (download PDFs), `convert` (PDF → markdown), `classify` (type-detect MDs into the extraction-schema buckets), `extract` (MD → structured JSON sidecar), `link` (cross-doc + intra-doc linker), `backfill` (registry-driven `*_normalized` slots and `Speaker.person_id`). Plus `es-init` to provision the Elasticsearch projection layer (templates, indices, aliases, API keys), `embed` to generate BGE-M3 dense_vector enrichments, `analyze` to run the four-prompt discourse-analysis pipeline (Hawkins / voice / DQI / V-Party) over substantive speeches via OpenRouter, `index` to project sidecars + enrichments into the live ES indices, and `query` for the typed query layer that backs the public search and LLM-agent tools.

### `fetch`

```sh
# single day
uv run monitorul-ii fetch 2026-04-29

# date range, custom output dir
uv run monitorul-ii fetch 2026-04-01 --until 2026-04-30 --out ./pdfs

# multi-year backfill, newest→oldest so a partial run leaves you with the recent stretch
uv run monitorul-ii fetch 2000-01-01 --until 2026-05-04 --reverse

# different Partea (default is II)
uv run monitorul-ii fetch 2026-04-29 --part IV

# bypass the proxy
uv run monitorul-ii fetch 2026-04-29 --no-proxy

# bypass the S3 mirror even when env vars are set
uv run monitorul-ii fetch 2026-04-29 --no-upload

# re-fetch every day's index regardless of DB cache (paranoid mode)
uv run monitorul-ii fetch 2026-04-01 --until 2026-04-30 --force

# pace requests — seconds between successful PDF downloads (default 0.5,
# never applied before the first download or on skips)
uv run monitorul-ii fetch 2026-04-29 --delay 1.0
```

PDFs land in `<out>/<YYYY-MM-DD>_MO-P<part>-<num>-<year>.pdf`. The date is baked into the filename so everything sorts chronologically. Re-runs skip files already on disk; in-flight downloads write to a sibling `.part` file and are renamed atomically only after the body fully streams, so an interrupt or crash never leaves a truncated PDF that future runs would mistake for complete.

Each per-request fetch retries up to 3 times with `1s → 2s → 4s` backoff for transient failures (5xx, 429, transport errors). 4xx-not-429, content-type mismatches, and parse errors fail fast with no retry. Failures are then classified: transient ones go to `status='failed'` and are auto-retried on the next run; permanent ones (server returns HTML instead of a PDF, or 4xx-not-429) go to `status='gone'` and are treated as terminal — re-running won't pound the same dead URLs forever. Pass `--retry-gone` to a future `fetch` run to reset every `gone` row back to `pending` if the source site restores missing documents.

### `convert`

```sh
# convert every PDF in a directory (skips files that already have a .md sibling)
uv run monitorul-ii convert pdfs/

# one or more specific files
uv run monitorul-ii convert pdfs/2026-04-29_MO-PII-47-2026.pdf

# shell globs work — the date is in the filename
uv run monitorul-ii convert pdfs/2026-04*.pdf

# re-convert files that already have a .md
uv run monitorul-ii convert pdfs/ --force

# skip the S3 mirror
uv run monitorul-ii convert pdfs/ --no-upload

# control conversion parallelism — default is CPU count; set 1 for strictly sequential
uv run monitorul-ii convert pdfs/ -j 4

# walk PDFs newest→oldest so a partial run leaves you with the most recent stretch
uv run monitorul-ii convert pdfs/ --reverse
```

Each `<basename>.pdf` produces `<basename>.md` next to it. The MD opens with a YAML frontmatter block (issue, year, part, published, plus best-effort `chamber`, `session`, `session_date`, `legislature` parsed from the first page), followed by the cleaned body text. Per-page running headers, page numbers, and image placeholders are stripped; soft line breaks are re-flowed; hyphenated word breaks are joined.

`-j N` (or `--workers N`) controls conversion parallelism — default is `os.cpu_count()`; set `-j 1` for strictly sequential. Throughput plateaus around `-j 8` on a 20-core box because the layout model is small per-PDF and past that you mostly add scheduling overhead. The CLI also forces `OMP_NUM_THREADS=1` / `ORT_INTRA_OP_NUM_THREADS=1` at startup so the outer worker pool doesn't compete with onnxruntime's auto-threading (3.8× speedup vs the unfixed defaults). Export those env vars yourself to override.

`--reverse` flips the processing order. PDF filenames are date-prefixed (`<YYYY-MM-DD>_MO-PII-...pdf`), so reversing the sorted list walks newest→oldest — same semantics as `fetch --reverse`. Useful for backfills where a Ctrl+C should leave you with the recent stretch already converted.

When the S3 vars are set, MDs mirror to the same bucket alongside the PDFs (flat layout, `Content-Type: text/markdown`). Idempotent in the same way as `fetch`: skip if the local `.md` exists, `head_object` before each upload.

#### Scanned-PDF triage

`pymupdf4llm` extracts a *text layer*, not a *visual layer* — for scanned PDFs whose pages are images with no embedded text, it silently produces near-empty markdown. To find PDFs in `pdfs/` that need an OCR backend instead (e.g. Mistral OCR), run the diagnostic script:

```sh
.venv/bin/python scripts/detect_scanned.py            # full sweep, writes scanned_candidates.csv
.venv/bin/python scripts/detect_scanned.py --rebuild  # regenerate CSV from cached probe state
```

The script probes each PDF in a child subprocess (PyMuPDF can SIGSEGV on malformed image-only PDFs; subprocess isolation keeps one bad file from killing the whole sweep) and classifies by `avg_chars_per_page` + `image_pages_pct`. On the current corpus this surfaces 12 OCR-tier candidates (9 `ocr_required`, 3 `ocr_recommended`) and 19 `hybrid` (mostly tiny 2-page cover-sheet + image-insert docs where OCR is usually not worth the cost). The `convert` command does **not** auto-route these to OCR — review `scanned_candidates.csv`, run those PDFs through your OCR backend of choice, and feed the resulting markdown back into the pipeline. See `docs/architecture.md` § "OCR triage" for the threshold rationale and corpus distribution.

### `classify`

Step 1 of the extraction pipeline (see [`docs/extraction-schema.md`](docs/extraction-schema.md)) — sweep MDs and tag each with one of the six document-type buckets defined by the schema (`plenary_stenogram`, `plenary_joint_session`, `committee_synthesis`, `report_facsimile`, `question_register`, `other`). Pure regex over the filename suffix + first 10 KB of body; runs over thousands of docs in seconds.

```sh
# classify everything; one JSONL row per doc to stdout, summary counts to stderr
uv run monitorul-ii classify pdfs/

# only emit docs that need human review (other-bucket + ambiguous classifications)
uv run monitorul-ii classify pdfs/ --outliers

# tighten the ambiguity threshold (default 0.2)
uv run monitorul-ii classify pdfs/ --outliers --ambiguity-threshold 0.05

# walk newest→oldest like the other subcommands
uv run monitorul-ii classify pdfs/ --reverse
```

Each row carries `top_type`, `top_score`, `second_type`, `second_score`, an `ambiguous` flag, the full `all_scores` map, and the list of `matched_signals` (which detection rules fired). `--outliers` filters to docs that classified as `other` *or* flagged `ambiguous` — those are the unknown unknowns the schema-discovery loop wants to inspect. Structural co-evidence (a joint session also matches the plenary-stenogram marker; an `R`-suffix report carries the joint-session marker from where it was received) is **not** counted as ambiguity — those are enriching signals, suppressed via a small compatible-runners-up rule. On the current 2300+ doc corpus the sweep produces zero `other` and zero ambiguous results.

### `extract`

Step 2 of the extraction pipeline (see [`docs/extraction-schema.md`](docs/extraction-schema.md), v1.13.0). Reads converted MDs, dispatches to a per-document-type extractor, and writes a `<basename>.extraction.json` sidecar next to each MD. Document type comes from the `classify` rules. v0.1 ships extractors for all six document types — **`question_register`**, **`plenary_stenogram`**, **`plenary_joint_session`**, **`committee_synthesis`**, and **`report_facsimile`** (the `other` bucket gets the fallback minimal body shape from the schema). Every typed document in the corpus now produces a strict-validated sidecar.

```sh
# extract every MD in a directory
uv run monitorul-ii extract pdfs/

# one specific MD
uv run monitorul-ii extract pdfs/2026-03-25_MO-PII-29-2026.md

# re-extract even when the existing sidecar's schema_version + extractor_versions match
uv run monitorul-ii extract pdfs/ --force

# override the classifier on a single doc (use sparingly — only when classify is wrong)
uv run monitorul-ii extract pdfs/oddball.md --type question_register

# walk MDs newest→oldest like the other subcommands
uv run monitorul-ii extract pdfs/ --reverse

# discovery loop: print one JSONL row per doc whose claimed_pct < 0.95 with the top-3 gap previews
uv run monitorul-ii extract pdfs/ --coverage-below 0.95

# fast path: backfill the schema 1.13.0 identity layer (id / content_fingerprint / slug)
# onto already-extracted sidecars without re-running the per-type extractors
uv run monitorul-ii extract pdfs/ --identity-only

# skip the S3 mirror
uv run monitorul-ii extract pdfs/ --no-upload
```

Each sidecar is a strict JSON Schema-validated dict with three sections:

- **Envelope** — `schema_version`, hierarchical `document_id` (`mo://YYYY/PART/ISSUE`), `content_sha` (sha256-truncated-12 over the body bytes), `document_type`, `metadata` projected from the YAML frontmatter, paths back to the source MD/PDF, and an `extraction` block carrying per-component `extractor_versions` plus an **`identity` sub-block** (schema 1.13.0+) with `record_id` (mirrors `document_id` — the mo-documents Elasticsearch doc id). Each id-bearing record in the body (every agenda item, activity, interpellation, question, committee meeting, committee agenda item, report) also carries its own `id` / `content_fingerprint` / `slug` triple — minted by the identity producer (v0.1.0) and **persisted slug-once**: re-extractions preserve the prior slug when `id` matches, locking published URLs across extractor iteration.
- **Body** — type-specific shape per the schema. `question_register` is a flat list of written-question records (addressee, questioner, registration number, topic, question text). `plenary_stenogram` and `plenary_joint_session` carry a richer shape: a `session` envelope (chair_segments, secretaries, attendance, format, opened/closed_at, outcome, special_procedure; joint-session adds `chambers_present`), an `agenda_items[]` array (28-value category enum with weighted resolution, sub-fields for government_confidence / government_hour / reexamination, primary_references via a 13-variant discriminated union — `bill / law / oug / og / chamber_resolution / parliamentary_resolution / motion / court_decision / constitution / regulation / eu_doc / treaty / code` plus `unknown` catch-all; v0.5.0 adds best-effort `subject` text on bill / law / parliamentary_resolution from the surrounding sentence — `privind X / pentru aprobarea X / referitoare la X / cu privire la X / asupra X` connectors, ±200 char window, null when no connector fires; per-item `activities[]`), and a sibling `interpellations[]` array (each entry: questioner Speaker, addressed_to, interpellation_number, topic, response_deferred flag, and as of v0.2.2 a `question_text` field that recovers the policy-substance body, a `response` field that pairs replying minister/secretar-de-stat turns with their questioner, and quote-aware `topic` detection that prefers `„...", «...», "..."` subjects over the first body line; v0.2.3 rewrites `addressed_to` capture so role-form mentions like `domnului NAME, ministrul X` recover the full ministry name instead of single-letter noise; chair turns / pure political-declaration turns / MO footer matches are filtered out as boilerplate so they don't pollute the array). Activities are one of `speech | vote | procedural | narrator | deferral`; speeches carry `delivery_mode` (tribune / from_floor / online / from_balcony / written) and `references_mentioned[]`; votes carry the 8-value motion_type (incl. `system_check` for hardware tests), 7-value voting_method (incl. `electronic_remote` for pandemic-era), `counts.for ∈ int|null|"unanimous"`, and `timing: live | deferred`. `committee_synthesis` is meeting-as-atom: a top-level `period: {start, end}` and a `committees[]` array; each committee carries `name`, `kind` (5-enum: permanent / special / inquiry / special_joint / inquiry_joint — v0.2.0 graduates joint Camera+Senat permanent committees like UNESCO / securitate națională / Statutul deputaților from `permanent` to `special_joint`), `chair` + `secretary` Speakers from the closing PREȘEDINTE / SECRETAR signature lines, and a `meetings[]` array. Each meeting has `dates[]` (1+ ISO dates harvested from "în zilele de **DD, DD ... month YYYY**"), `time_windows[]` (HH:MM ranges), `format` (`in_person | online | mixed | null`), `purpose` (`documentare_consultare | dezbatere_decizie | aprobare_raport | audiere_candidați | null`), `joint_with[]` of `{name, chamber}` (multi-committee `în comun cu Comisia X[, Y, Z] din [Camera Deputaților|Senat]` clauses; the parser splits the list by `Comisia` starts so multi-clause names like `Comisia juridică, de disciplină și imunități a Camerei Deputaților` stay intact, rejects joint-bill-review false positives where the trigger is preceded by `raport / aviz / sesizare / fond / studiu` — populated on 31% of c-suffix corpus, 891 entries), `roster[]` of `{speaker, mode, intra_committee_role, substituted_by}` (three format sub-parsers run additively and merge by name: tabular `|Name|Prezent fizic|` / `|Name|Prezent la sediul CD|` for 2022/2024+, narrative `au fost prezenți: A, B, C` / `Și-au înregistrat prezența la lucrări următorii deputați:` / `au fost prezenți N deputați, și anume:` for 2018+, per-day numbered `1. NAME, Grupul parlamentar al X` for 2008-era; populated on 93% of c-suffix corpus, 124K total entries — physical 111K, online 1.7K, absent 10K, substituted 676), and an `agenda[]` array of items each with `ordinal`, `title`, `primary_references[]` (bill cites with the 1990–2100 year guard), `committee_role` (fond / aviz / fond_comun), `output_type` (raport / raport_preliminar / raport_suplimentar / raport_comun / raport_comun_suplimentar / aviz / studiu / proiect_de_opinie / amânare), `outcome_text`, and a best-effort `vote_summary` (outcome + majority + integer counts). v0.2.0 adds tabular-agenda fallback for the 2024+ `|Nr.|PL-x|Titlu|Scopul|Rezoluție|`-style table format that the numbered-narrative regex previously missed (88% of 2024+ committees now have populated agenda_items). v0.2 still leaves per-day meeting splits and tabular roster role inference deferred — see `docs/architecture.md`. `report_facsimile` is intentionally minimal: a `report` block (title, issuing_body, reporting_period: {start, end}, received_at: {session_kind, session_date, received_in_document}), a `headings[]` outline (level + text + line), and a `raw_markdown_excerpt` (first 500 chars). The full report stays in the sidecar markdown — these reports are reproduced verbatim per the spec.
- **Coverage** — diagnostic block: `body_chars`, `claimed_chars`, `claimed_pct`, plus `gaps[]` (unclaimed spans > 20 chars with line numbers and a 200-char preview) and `claimed_by_policy[]` (boilerplate the extractor intentionally skipped, with reason). Diagnostic-only — never gates writes.

`--force` overrides the version-aware idempotency gate. By default, an existing sidecar is reused if its `schema_version` and every `extractor_versions` key match the current code; mismatches re-extract automatically. This is the primary mechanism for "we bumped the speakers parser, re-extract everyone": bump `SPEAKERS_VERSION`, re-run `extract`, and only the affected docs regenerate.

`--type TYPE` is the override for the rare doc that classifies wrong (e.g., a stenogram with an unusual header that confuses the `(STENOGRAMA)` detector). Don't reach for it during routine runs — it bypasses the classifier as the source of truth.

`--coverage-below MARGIN` is the discovery-loop entry point. It doesn't change writes — every MD is extracted normally — but in addition prints one JSONL row per doc whose `claimed_pct < MARGIN` to stdout, with the doc's path, doc_type, claimed_pct, gap_count, and the top-3 gap previews. Same UX as `classify --outliers`. Lowering the threshold over time is how you find the next pattern the extractor needs to cover.

`--identity-only` is the fast path for backfilling the schema 1.13.0 identity layer. Instead of re-running every per-type extractor (which on the 5552-doc corpus is a ~30-minute job), `--identity-only` reads the existing sidecar's body verbatim, runs only `assign_identity` over it, bumps `extractor_versions.identity` and `schema_version` to 1.13.0, and rewrites atomically — taking seconds per doc and producing the exact same downstream identity surface a full re-extract would. Idempotent on the identity version: a second `--identity-only` run with `force=False` skips when the identity version is already current. Combine with `--force` to re-mint identities under updated rules. Slug-once still applies — slugs are read from the on-disk sidecar before re-minting and survive the upgrade.

When the S3 vars are set, sidecars mirror to the same bucket as the PDFs/MDs (flat layout, `Content-Type: application/json`). Idempotent in the same way: skip if local sidecar matches, `head_object` before each upload.

Schema validation runs *pre-write*: a sidecar that doesn't validate against the canonical schema (`src/monitorul_ii/extraction_schema.json`) never lands on disk. The rejected dict is dumped to `<basename>.rejected.json` for inspection so you don't have to re-derive it from logs.

### `link`

Linker — runs **three passes** by default: (1) **report→session** (cross-doc), fills each `report_facsimile` sidecar's `received_at.received_in_document` back-pointer with the matching joint-session (or single-chamber) stenogram's `document_id`; (2) **vote-pair** (cross-doc, linker v0.2.0+), pairs deferred votes (`outcome=deferred`) in stenogram N with their resolving votes in a later stenogram M, writing `defers_to` (forward link) on the deferring vote and `resolves[]` (back-link, list of origin document_ids) on the resolver; (3) **xref / cross-reference** (intra-doc, xref_linker v0.1.0+), resolves bare `art. N` unknown references in plenary sidecars to the most-recent preceding non-unknown anchor IN THE SAME REFERENCE LIST (`primary_references[]` / `references_mentioned[]`), writing `unknown.resolved_to.char_offsets`. Run it after `extract`: extract writes report sidecars with `received_in_document: null`, plenary votes with `defers_to: null` / `resolves: []`, and unknown references with no `resolved_to` field; link walks all sidecars and fills the slots.

```sh
# link every sidecar in a directory (all three passes)
uv run monitorul-ii link pdfs/

# preview without writing anything
uv run monitorul-ii link pdfs/ --dry-run

# re-link sidecars whose targets are already populated
# (useful after a cohort re-extract; applies to all selected passes)
uv run monitorul-ii link pdfs/ --force

# run only the report→session pass
uv run monitorul-ii link pdfs/ --report-only

# run only the vote-pair pass
uv run monitorul-ii link pdfs/ --vote-only

# run only the cross-reference (art-N) pass
uv run monitorul-ii link pdfs/ --xref-only

# skip the S3 mirror (otherwise modified sidecars re-upload)
uv run monitorul-ii link pdfs/ --no-upload
```

Why a separate subcommand and not part of `extract`? The cross-doc passes need the global picture (scan all sidecars to build the session + vote indexes), while extract is single-pass per-MD. The xref pass is per-sidecar but lives next to its sister passes for ergonomic reasons (one CLI to remember; one re-run after extract; same atomic-write + idempotency contract). Keeping them separate from extract preserves extract's "single source of truth for body content" contract and lets each pass run independently.

**Pass 1 — report→session** indexes plenary sidecars by `metadata.session_date` (joint sessions beat single-chamber on the same date) and matches each report's `received_at.session_date`.

**Pass 2 — vote-pair** derives a match key per vote from the parent agenda item's `primary_references[]`: a `bill` cite (`f"bill:{number}/{year}"`) wins; failing that, a motion title hash for motion-class votes carrying a quoted title ≥12 chars. Other ref types (`law`, `oug`, `og`, `parliamentary_resolution`, `chamber_resolution`) are intentionally excluded — they collide unrelated bills sharing the same underlying cite (e.g., `law:47/1992` for every CCR-referral procedural note); see the linker module docstring for the false-positive analysis. Matching is window-bounded (`DEFERRAL_WINDOW_DAYS=60`) and earliest-resolver-wins. Multi-deferral chains: when A defers to B and B itself defers to C, the chain reverses on the back-link — `C.resolves = [A, B]`.

**Pass 3 — xref / cross-reference** walks every plenary sidecar's reference lists and, for each `unknown` reference whose `raw` matches `art. N` / `Art. N` / `articolul N`, picks the most-recent-preceding non-unknown reference from THE SAME LIST as the anchor and writes its `char_offsets` into the unknown's `resolved_to` field. Same-list scoping is a hard constraint: references parsers (`parse_primary_references` / `parse_mentioned_references`) emit offsets LOCAL to their parent string (agenda title or speech text), so cross-list comparison would compare nonsensical coordinate systems. Tie-break at equal end offsets prefers `code` > `law` > `bill` > the rest. Cross-list resolution (e.g., agenda-level bill cite anchoring article references in child speeches) is deferred to a future v0.2 of the xref linker — that needs an agenda-aggregation index, not just same-list scope. Production sweep on the 5,551-doc corpus: 18.5% of art-N unknowns (20,433 / 110,453) gain a `resolved_to` value; 81.5% stay null (cross-list cases out of scope for v0.1, plus genuine no-anchor cases like SUMAR-area citations whose owning law cite never appeared in the same list). Spot-check on 20 random resolved entries: ~85% precision (17/20). The xref pass also bumps the sidecar's `schema_version` to 1.12.0 on each successful write — the new schema's additive `resolved_to` field requires it, and bumping forward is safe (1.12.0 is fully backwards-compatible with 1.11.0).

`--report-only`, `--vote-only`, and `--xref-only` are mutually exclusive selectors for a single pass; the default is to run all three.

`--force` re-links populated entries — by default already-linked sidecars are skipped with reason. The xref pass goes one step further under `--force`: it also CLEARS stale `resolved_to` values when the new run finds no anchor under the current rules, making `--force` the canonical "re-resolve under current scope" command. `--dry-run` prints what would be linked without modifying any files.

Pre-write schema validation runs on every linked sidecar — an invalid post-link shape is rejected and the file is NOT touched. Atomic write via `.part` rename, same contract as `extract`.

When S3 env vars are set, every successfully-linked sidecar re-uploads with `Content-Type: application/json` and **overwrites the bucket copy** — the underlying `Uploader.upload_if_missing` takes an `overwrite=True` kwarg that the link / backfill / extract paths thread through, so modifications to mutable artefacts (sidecars rewritten in place) reach the bucket, not just the local filesystem. PDF / MD uploads keep the default `overwrite=False` because those bytes are immutable per filename — the head-object gate skips wasted PUTs on resume runs. `--no-upload` disables the S3 mirror entirely.

All three passes are idempotent: re-extracting a sidecar (extractor version bump → re-extract) clobbers linker-written fields, but a quick `monitorul-ii link` recovers. The cross-doc passes are fast (~1ms per doc — pure dict lookup); the xref pass is also fast (single walk per sidecar, no MD body access). On the 5,551-doc corpus, a second run yields zero new resolutions — idempotency is corpus-verified for the xref pass.

### `backfill`

Registry-driven backfill — fills the schema's `*_normalized` slots by joining curated registries against the raw values that `extract` already pulled. Same architectural shape as `link`: read sidecars, look up each raw value in an in-memory registry index, write the canonical id back atomically with pre-write schema validation. Each `--kind` targets one registry/field pair.

```sh
# run every shipped backfill pass over a directory
uv run monitorul-ii backfill pdfs/

# only the institutional-bodies pass (report_facsimile.issuing_body_normalized)
uv run monitorul-ii backfill pdfs/ --kind=issuing_body

# only the persons pass (Speaker.person_id from persons.json)
uv run monitorul-ii backfill pdfs/ --kind=persons

# preview without writing anything to disk or S3
uv run monitorul-ii backfill pdfs/ --dry-run

# overwrite existing *_normalized values when the registry now resolves a different id;
# on the persons pass, --force ALSO clears stale Speaker.person_id values when the
# matcher now returns no match (recovery path after a precision-improving matcher
# change, e.g. the per-token fuzzy tier rejecting an old joined-Lev≤2 false positive)
uv run monitorul-ii backfill pdfs/ --force

# skip the S3 mirror (otherwise modified sidecars re-upload)
uv run monitorul-ii backfill pdfs/ --no-upload

# parallelize across cores (default: CPU count) — each worker rebuilds the matcher's
# alias index once and processes its slice
uv run monitorul-ii backfill pdfs/ -j 16

# force serial execution (deterministic completion order; helpful when debugging)
uv run monitorul-ii backfill pdfs/ -j 1

# also re-upload sidecars where every slot already carries the current canonical
# id (status=skip, reason=already filled) — closes the historical bucket-staleness
# gap from pre-overwrite-fix runs
uv run monitorul-ii backfill pdfs/ --reupload-on-skip
```

`--kind` selects which pass to run; choices are `issuing_body` (fills `report_facsimile.body.report.issuing_body_normalized` from the institutional-bodies registry), `ministry` (fills `question_register.body.questions[].addressee.ministry_normalized` and `plenary_*.body.interpellations[].addressed_to_normalized` from the ministries registry, with institutional-body fallback for non-ministry addressees like `Curtea de Conturi`), `proposed_by` (fills `plenary_*.body.agenda_items[].activities[].proposed_by` with the canonical `Guvern` Speaker on votes whose parent agenda carries an OUG/OG cite — Government Ordinances are by definition government-proposed), `persons` (fills `Speaker.person_id` across every Speaker dict in the body — chair, agenda activities, interpellation questioner / response, committee roster, signatures, qr questioners — by joining against the curated `persons.json` registry of Romanian parliamentarians), and `all` (default — runs every shipped pass; forward-compatible with future registries). `--force` overwrites `*_normalized` values that disagree with the registry's current canonical id; without `--force`, conflicts skip with reason. `--dry-run` reports planned changes (one line per sidecar showing the raw value, the canonical id, and the matched-via tier) and exits without writing. `-j N` / `--workers N` runs the per-sidecar match loop in a `ProcessPoolExecutor` (default: `os.cpu_count()`); each worker is a spawn-context child interpreter that builds the matcher's lru-cached alias index once (~1-2s startup per worker) and then processes its slice — output appears in completion order, not input order. Set `-j 1` to fall back to the serial generator path (deterministic ordering, no spawn overhead). Real-world speedup at 200 sidecars / 16 workers vs 1 worker: ~2.5×; the matcher itself isn't the only cost — JSON parse + schema validation + atomic rewrite also dominate per-sidecar work, so super-linear scaling isn't possible. `--reupload-on-skip` is a precision-targeted repair flag for the historical bucket-staleness gap: when set, sidecars that skip with reason `already filled` (every slot already carries the current canonical id, no local rewrite needed) ALSO re-upload with `Content-Type: application/json` and `overwrite=True`. This closes the gap left by pre-overwrite-fix runs that wrote modifications locally but didn't propagate them to S3 because `upload_if_missing` short-circuited on the existing key. Other skip reasons (`no speakers in body`, `no raw value`, `no registry match`, `no government-proposed agendas`) stay upload-skipped — those mean the local file simply doesn't carry data this pass would emit, so the bucket can't be "stale" relative to one. No effect under `--no-upload` or when S3 isn't configured.

**Institutional-bodies registry** (30 entries: CSAT, SRI, SIE, STS, BNR, ICR, Avocatul Poporului, Consiliul Legislativ, SRTv, SRR, ANCOM, ANRE, Curtea de Conturi, Curtea Constituțională, ANI, ASF, ANSPDCP, CSM, ONPCSB, AGERPRES, ANAD, AEP, ICCJ, CCIR, CNA, CNSAS, CNCD, ANRM, ANCPI, SPP). Production smoke on the 52 R-suffix corpus (post `report_facsimile.py` v0.2.0): 52/52 (100%) of R-suffix sidecars resolve to a canonical id — the v0.1.0 18-doc gap (mostly ANCOM `pentru anul`, ANRE `<br>` linebreak residue, AEP election-day `din [DD] month YYYY` forms) closed by the extractor's surface-form expansion.

**Ministries registry** (30 entries — one id per "ministry concept" with historical names + Romanian genitive declensions as aliases: prime_minister, health, culture, education, research, transport, environment, finance, foreign_affairs, economy, energy, regional_dev, youth_sport, communications, labor, justice, eu_funds, tourism, family, internal_affairs, defense, romanians_abroad, agriculture, sme, infrastructure_projects, delegated_water, delegated_budget, delegated_higher_education, secretariat_general, secretariat_revolutionaries). Production smoke (post `interpellations.py` v0.2.3 `addressed_to` rewrite): qr questions resolve at **92.7%** (1851/1997 non-null raws); plenary interpellations resolve at **89.4%** (1849/2068 non-null raws — was 25% pre-v0.2.3, before the upstream extractor was fixed to capture full ministry / role-form / PM strings instead of single-letter noise).

**proposed_by pass** is signal-driven, not registry-driven: there's no curated table of bill sponsors (the Tier 4 prompt scoped that to ~10K bills, requiring per-bill metadata that lives on parlament.ro). Instead, the pass attributes votes to the Government when the parent agenda carries an `oug` / `og` reference OR the title matches an OUG/OG cite pattern (`OUG nr. X/Y`, `Ordonanței Guvernului nr. X/Y`, `O.U.G.`, `O.G.`). Government Ordinances are by definition government-issued, and any vote on a bill approving one inherits that proposer. Production smoke: 10.76% of plenary votes (5330/49556) attributed to Guvern; 554 of 4446 plenary sidecars touched (post-`agenda.py` v0.2.5 re-baseline; the previous figure of 14.2% / 7018 votes was inflated by misattributed OUG/OG cites in contaminated agenda titles, which the title-contamination clip now removes). The remaining ~89% are PL-x / L bills proposed by parliamentary groups / individual MPs / committees — that long tail requires a parlament.ro per-bill scrape and is deferred (see `docs/architecture.md` § Future graduation candidates).

**Persons registry** (curated `persons.json` of Romanian parliamentarians, presidents, prime ministers, and ministers — Q4 of `docs/elasticsearch-indexing.md`). Each entry has `id` (kebab-case `<surname>-<given_name>`), `canonical_name`, `diacritic_form`, `aliases` (covering name-order variants, mojibake forms observed in pre-2018 PostScript-converted PDFs, and common nicknames), `wikidata_qid`, `birth_date`, `mandates[]` (role / chamber / legislature / from / to / party), and a `homonym_disambiguation` slot for the long-tail. The shipped registry carries **13,479 entries**: 51 hand-curated leadership-tier seeds + 9,014 Wikidata-bulk-imported politicians (every entry QID-verified) + 4,414 corpus-derived stubs minted by `tools/add_unresolved_speakers.py` to give every unique corpus speaker a stable `person_id` even before its Wikidata enrichment lands. Stubs carry `wikidata_qid: null` / `mandates: []`; they're forward-extensible — the operator can attach QIDs / mandates over time via `enrich_persons_wikidata.py --apply` or manual review against `tools/inspect_speaker.py` output. Coverage on a 1,526-speaker 2024 stenogram: **99.9%** post-stub-merge. The matcher peels honorific (`Domnul/Doamna`) and parliamentary-title (`deputat/senator/ministru/...`) prefixes plus trailing role clauses (`, vicepreședinte al Camerei`) before tier matching. Tiers: exact → case-insensitive → **aggressive** diacritic+mojibake fold (collapses `V„c„roiu`, `Vãcãroiu`, `Văcăroiu` onto the same key — see `_strip_diacritics_aggressive` for the substitution map) → token-set (orderless, splits on whitespace + hyphens so `Sorin-Mihai` and `Sorin Mihai` collide) → **fuzzy** Levenshtein ≤ 2 (justified for human names per the design doc — absorbs OCR slips like a missing-letter typo). Homonym disambiguation: when two registry entries share the same canonical_name / alias surface form, the matcher uses the MO `metadata.year` to pick the entry with an active mandate that year; if no entry's mandate covers the year, the matcher refuses to guess and returns null — the unresolved Speaker stays visible via `Speaker.raw` and surfaces in the long-tail report. Procedural / institutional non-canonical labels (`Din sală`, `Guvernul`, `<chair narration>`, `Voci`, `Aplauze`) are matched against an explicit denylist and never resolve.

Match strategy on the institutional / ministry registries is exact → case-insensitive → diacritic-stripped (cedilla `ţ`/`ş` collapses to comma `ț`/`ș`; mojibake replacement chars `�` strip out) → token-set → **prefix** (ministries only — last-resort longest-prefix match, where the cleaned input starts with a registered alias at a whitespace boundary; recovers from the plenary extractor's habit of bleeding sentence prose into `addressed_to`, without admitting fuzzy matches). The persons matcher is the only one that admits a fuzzy tier, since human-name OCR errors are the dominant failure mode there and the registry is small enough to keep the precision/recall trade-off favourable.

Backfill versions are NOT part of `extractor_versions` — re-extracting a sidecar clobbers backfill-written fields. Re-running `backfill` after `extract` recovers them; backfill is fast (in-memory dict lookup per record).

### `es-init`

Provision the Elasticsearch projection layer that powers `monitorul.ai`. Idempotent — re-running detects existing entities and no-ops. See [`docs/elasticsearch-indexing.md`](docs/elasticsearch-indexing.md) for the full design (Q1–Q9), and [`docs/elasticsearch-indexing-prompts.md`](docs/elasticsearch-indexing-prompts.md) for the per-phase rollout.

```sh
# print the plan without contacting ES (no env vars needed)
uv run monitorul-ii es-init --dry-run

# live bootstrap against the cluster pointed at by ES_URL / ES_API_KEY
uv run monitorul-ii es-init

# script a major-trigger blue-green rebuild with an explicit generation
uv run monitorul-ii es-init --generation-suffix 20260615-v2

# bootstrap without leaving the smoke-test document in mo-documents
uv run monitorul-ii es-init --skip-smoke
```

The bootstrap installs four sets of entities:

1. **Component templates** — `mo-analyzers` (the custom `romanian_folded` + `romanian_exact` analyzers; pairs with ES's built-in `romanian` analyzer for diacritic-insensitive and exact-phrase queries respectively) and `mo-common-fields` (the `record_id` / `document_id` / `content_fingerprint` / `indexed_at` / `extractor_versions` / `enrichment_versions` / `schema_version` keystone fields shared by every grain except `mo-persons`).
2. **Index templates** — one per grain, composing the component templates above and adding the grain-specific properties from the v1 mappings under `src/monitorul_ii/elasticsearch/mappings/`. Nine grains: `mo-documents`, `mo-agenda-items`, `mo-speeches`, `mo-votes`, `mo-interpellations`, `mo-questions`, `mo-committee-meetings`, `mo-reports`, `mo-persons`.
3. **Indices** — one concrete index per grain with blue-green naming `<grain>-<YYYYMMDD>-v1`. Two aliases ride on the `create` request so they appear atomically: a read alias `<grain>` (used by Next.js + the LLM-agent layer) and a write alias `<grain>-write` (used by the indexer; carries `is_write_index: true` so multi-generation catch-up writes are unambiguous). When the read alias already points at a live index, the bootstrap leaves it alone — that's how the major-trigger lifecycle (Q6) and the routine "the indices already exist" path stay in one codepath.
4. **API keys** — `monitorul_reader` (read-only on `mo-*`, no scripting / scroll / SQL / cluster info — used by Next.js's `lib/search.ts`) and `monitorul_indexer` (read+write+create+manage on `mo-*` + cluster `monitor` — used by the indexer + bootstrap). The encoded key value is **only** returned at creation time and is printed once to stdout — capture it before the terminal scrolls. ES will not return it again; if you lose it, invalidate the key and re-bootstrap (the helper detects invalidated keys as absent and mints fresh).

`--dry-run` short-circuits before any client construction — it doesn't read `ES_URL` / `ES_API_KEY`, so it's safe for CI sanity checks.

The bootstrap finishes with a smoke index/get round-trip on `mo-documents` (`_id="mo://test/PII/0"`, `refresh="wait_for"`) so a successful exit means end-to-end wiring works. `--skip-smoke` opts out — useful when you want to validate the templates + aliases shape without leaving the smoke document behind.

API-key creation is **non-blocking**: when ES refuses to mint role-scoped keys (e.g. the bootstrap `ES_API_KEY` is itself a derived API key, which ES locks out from creating keys with explicit privileges), the bootstrap surfaces the failure as a warning and proceeds to the smoke round-trip. Templates + indices are the load-bearing wiring; the smoke confirms they work. The exit code is non-zero (`1`) so the operator notices, and the warning instructs them to re-run with a primary credential (a username/password or a non-derived API key) to mint the `monitorul_reader` / `monitorul_indexer` keys.

### `embed`

Generate BGE-M3 (1024-dim) dense_vector embeddings for every embeddable record across all six document types — substantive speeches (`text_length ≥ 100`), agenda item titles, interpellation `topic + question_text`, qr questions, committee meeting purposes, and report titles + headings. Vectors persist as `<basename>.embedding.bge-m3.v0_1.json` enrichment files alongside each sidecar; the `index` step picks them up automatically and projects them onto `enrichments.embedding` (the dense_vector field) and `enrichments.embedding_text_fingerprint` (the staleness sentinel) for every grain that supports kNN retrieval. Run **after** `extract` / `link` / `backfill`, **before** `index`. Pairs with the FastAPI service in [`services/embed/`](services/embed/) — see that directory's README for deployment.

```sh
# Embed one sidecar (service must be running on http://127.0.0.1:8000)
uv run monitorul-ii embed pdfs/2018-11-20_MO-PII-168-2018.extraction.json

# Embed every sidecar under pdfs/ — idempotent, fingerprint-skip
uv run monitorul-ii embed pdfs/

# Dry-run: walk the records and report counts without contacting the service
uv run monitorul-ii embed pdfs/ --dry-run

# Re-embed everything regardless of fingerprint match (after a model bump)
uv run monitorul-ii embed pdfs/ --force

# Point at a remote / GPU service
uv run monitorul-ii embed pdfs/ --embed-url http://gpu-host:8000

# Override the per-request batch size
uv run monitorul-ii embed pdfs/ --batch-size 16
```

Flags:

- `--force` — re-embed every record regardless of `text_fingerprint` match. Pair with a model bump (the producer/version constants change) or after the producer's normalisation rules change. Without `--force`, every per-record entry whose fingerprint matches the current text reuses its prior vector verbatim — no HTTP call, no file rewrite.
- `--dry-run` — walk the sidecars and report which records would be embedded vs reused, without contacting the service or writing files. Useful for "how much will this cost / take?" planning before kicking off a bulk run.
- `--embed-url URL` — embedding service base URL (default: `$EMBED_URL` env var, then `http://127.0.0.1:8000`). The service must respond to `GET /healthz` (liveness probe — used by the CLI to fail fast on a misconfigured endpoint) and `POST /embed` (the actual encoding call).
- `--batch-size N` — texts per HTTP request to the embed service (default: 32). Higher values reduce HTTP overhead at the cost of larger request bodies; the service may re-batch internally to stay under GPU memory.
- `--no-upload` / `--bucket NAME` — same S3 mirror flags as the other subcommands. When the S3 env vars are configured, modified embedding files mirror to the bucket with `Content-Type: application/json` and `overwrite=True` (embedding files are mutable per-record — fingerprint-mismatched entries are rewritten in place).

Idempotency contract: each entry's `text_fingerprint` is the 12-char sha256 of the NFC + whitespace-collapsed text — the same shape as the identity layer's `compute_content_fingerprint`. On re-run, fingerprint-matched entries reuse the existing vector verbatim. When a re-extract changes the speech text, the next `embed` pass re-embeds only the records whose fingerprint mismatched; the rest stay verbatim. Hybrid search never serves a vector that doesn't match its text — at index time the indexer projects both `enrichments.embedding` (the vector) and `enrichments.embedding_text_fingerprint` (the keyword sentinel) so the query layer can detect stale vectors and exclude them from kNN until re-embedded.

Long-tail handling (Q8 v0.1): texts beyond `MAX_TEXT_CHARS = 8000` (≈2K BGE-M3 tokens) are truncated to the first 8000 characters, with `_meta.truncated: true` flagged on the entry so the operator can audit the long-tail size. v0.2 will ship proper chunking with `record_id#chunk-N` keys; v0.1 lets us bootstrap the corpus and the long-tail under-represented in semantic search is documented in [`docs/architecture.md`](docs/architecture.md).

The producer is sequential — embedding throughput is dominated by the service-side compute, not the producer-side I/O. The CLI does **not** support `-j N` for parallel sidecars; if the service is GPU-backed, run multiple `embed` processes pointed at the same service rather than threading inside one process. Bootstrap timing per Q8: ~3 hours on a single consumer GPU, ~30 hours on CPU. After the bootstrap, the daily-cron run typically embeds 1–5 new MO sidecars (~5 minutes).

### `analyze`

Run the four-prompt **discourse-analysis pipeline** (Hawkins populism → voice attribution → DQI deliberative quality → V-Party + V-Dem anti-pluralism) over every substantive speech in the corpus. Per-speech outputs persist as `<basename>.discourse.flash-lite.v0_1.json` enrichment files alongside each sidecar; the `index` step picks them up and the denormaliser flattens the payload onto `mo-speeches.enrichments.discourse.{hawkins,voice,dqi,vparty}.*` (see `docs/elasticsearch-indexing.md` § Q3 / Q5 + `docs/discourse-pilot-baseline-2026-05.md` § 11 for the four-cell Hawkins × V-Party design rationale). The denormaliser projects both the **aggregates** (`score`, `framework_confidence`, `marker_count`, `marker_kinds`, `dominant_voice`, DQI sub-codings) and the full **per-marker arrays** — `markers[].{kind, marker_confidence, rationale_short, evidence.text, evidence.char_range}` for Hawkins / V-Party / DQI, and `voice.classifications[].{marker_id, voice, voice_confidence, attributed_to, rationale_short, voice_evidence.text, voice_evidence.char_range}` — plus the framework-level `rationale` and `framework_version`. `evidence.char_range` is computed at index time via the producer's typography-tolerant matcher, so the web app can render the markers and highlight each evidence anchor inside the speech text without porting the Romanian-folding logic to JS. Calls are dispatched to OpenRouter against `google/gemini-3.1-flash-lite` (the model picked by the calibration sweeps); strict `json_schema` response_format is attempted first with an automatic fallback to `json_object` for backends that reject the schema (Gemini's well-known limitation). Run **after** `extract` / `link` / `backfill` / `embed`, **before** `index`.

```sh
# Analyse one sidecar
uv run monitorul-ii analyze pdfs/2024-04-15_MO-PII-50-2024.extraction.json

# Analyse every sidecar under pdfs/ — idempotent, fingerprint-skip
uv run monitorul-ii analyze pdfs/

# Dry-run: walk the records and report counts without contacting OpenRouter
uv run monitorul-ii analyze pdfs/ --dry-run

# Re-code everything regardless of fingerprint match (after a prompt-version bump)
uv run monitorul-ii analyze pdfs/ --force

# Walk newest→oldest and stop after 10 sidecars
uv run monitorul-ii analyze pdfs/ --reverse --limit 10

# Cap cumulative spend at $100 (in-flight sidecar finishes atomically, then exits)
uv run monitorul-ii analyze pdfs/ --reverse --budget-usd 100

# Parallel — threads scale near-linearly on OpenRouter round-trips
uv run monitorul-ii analyze pdfs/ -j 16

# Skip speeches over 500 words (default is 800; v0.2 will chunk instead)
uv run monitorul-ii analyze pdfs/ --max-words 500
```

Flags:

- `--force` — re-code every record regardless of `text_fingerprint` match. Pair with a prompt-version bump (the per-framework `PROMPT_VERSIONS` constants change) or after the producer's normalisation rules change. Without `--force`, every per-record entry whose fingerprint matches the current speech text reuses its prior payload verbatim — no API call, no file rewrite.
- `--dry-run` — walk the sidecars and report which records would be coded vs reused, without contacting OpenRouter or writing files. Useful for "how much will this cost / take?" planning before kicking off a bulk run. Short-circuits the API-key / health-check entirely so it's safe in CI.
- `--limit N` — process at most N sidecars (after `--reverse` is applied). Default: all collected sidecars. Convenient for spike runs ("only the last 5 sidecars").
- `--reverse` — walk newest→oldest like `extract` / `embed`. Combined with `--limit` this gives "most recent N sidecars first" — the right shape for backfilling the recent corpus before the long tail.
- `--openrouter-url URL` — OpenRouter base URL (default: `$OPENROUTER_URL` env var, then `https://openrouter.ai/api/v1`). The CLI smoke-tests `GET /models` before walking the sidecar list so a misconfigured endpoint or bad API key fails fast.
- `--retry-on-error N` — retry budget per LLM call with exponential backoff (1, 2, 4, 8s, capped at 8s). Covers transport / `json_parse` / `schema_invalid`; configuration errors (missing API key) short-circuit. Default: 1.
- `--max-words N` — skip speeches above this many words with reason `text_too_long` (default: 800). v0.2 will introduce chunked coding (`record_id#chunk-N`); v0.1 defers them. Tune up only if your average speech is short and you want to absorb more long-tail at the bottom of the budget.
- `--budget-usd N` — soft cap in USD on cumulative estimated spend across the current run. Per-call cost ≈ `tokens_in × $0.10/M + tokens_out × $0.40/M` (Flash-Lite conservative bounds; constants live near the top of `discourse.py`). When the cap is hit, the in-flight sidecar finishes atomically and the producer exits cleanly (exit 0). **Resume by re-running** — fingerprint-match skips already-coded records at zero API cost. The dominant operator pattern: launch a background run with `--budget-usd 100`, walk away, come back the next day, re-run to mop up anything that was in-flight.
- `-j N` / `--workers N` — parallelism via `ThreadPoolExecutor` (default: 1). Threads, not processes — discourse calls are network-bound and GIL-friendly via httpx. Each worker shares the OpenRouter HTTP client (urllib3 connection pool is thread-safe). 20-core box: `-j 16` lifts ~12 sidecars/min (sequential) to ~50–80 sidecars/min, bounded by OpenRouter's per-key rate limit.
- `--no-upload` / `--bucket NAME` — same S3 mirror flags as the other subcommands. When the S3 env vars are configured, modified discourse files mirror to the bucket with `Content-Type: application/json` and `overwrite=True` (discourse files are mutable per-record — fingerprint-mismatched entries are rewritten in place).

Idempotency contract: each entry's `text_fingerprint` is the 12-char sha256 of the NFC + whitespace-collapsed speech text — the same shape as the identity layer's `compute_content_fingerprint` and the embedding producer's fingerprint. On re-run, fingerprint-matched entries reuse the prior payload (Hawkins / voice / DQI / V-Party outputs) verbatim. When a re-extract changes the speech text, the next `analyze` pass re-codes only the records whose fingerprint mismatched. Each entry's `_meta` block carries `prompt_versions` (`hawkins=v1, voice=v1, dqi=v1, vparty=v2`); a future prompt-version bump will require `--force` to invalidate prior payloads.

Pipeline order per speech: **Hawkins → voice (conditional) → DQI → V-Party**. The voice classifier only runs when Hawkins emits at least one marker — if Hawkins says "no populist markers", voice has nothing to attribute. DQI and V-Party are independent classifiers and always run. The four prompts cost on average ~$0.005 / speech at Flash-Lite rates; the calibration sweep's $0.00493 / speech baseline implies ~$94 for the ~19,200 substantive speeches in the March 2023 → present window, comfortably under the $100 budget cap. Reads `OPENROUTER_API_KEY` from environment / `.env`.

Hawkins / DQI markers carry an `evidence.text` string the model claims is verbatim from the speech; the producer recovers char offsets via a typography-tolerant matcher (NFC + lowercase + whitespace-run-collapse + Romanian-quote / unicode-dash / ellipsis folding) so the model's typography drift (curly-vs-ASCII quotes, em-dash vs hyphen, joined newlines, sentence-leading capital → lowercase) doesn't drop voice runs. When even tolerant matching fails — real paraphrase, not just typography — the marker is still forwarded to the voice classifier without `char_range`; voice has `marker_text` and the prompt explicitly says hints are signals not commitments. Pre-fix the producer dropped paraphrased markers and silently skipped voice → entries marked failed and dropped from the persistent file; corpus smoke surfaced ~12% of records hitting this mode on Flash-Lite.

Two schema-aware salvage passes run when strict `jsonschema.validate` rejects an output. (1) Drop `markers[]` entries whose `kind` isn't in the schema's enum (closes the Gemini drift mode where voice-classifier values like `"quoted"` leak into Hawkins / V-Party `markers[].kind`). (2) Recursively strip object keys not declared in `properties` when the schema sets `additionalProperties: false` (closes DQI drift modes like the hybrid `respect_for_constructive_politics`, stray `text_2` / `text_3` evidence keys, etc.). Both passes are conservative: they only DROP things, never invent missing required fields. Salvage events bump the per-call `repaired` counter (alongside json-repair recoveries) so `tools/analyze_progress.py` shows them under the "recovery counters" section.

Two further fixes target the runaway-output tail. **(1)** `DEFAULT_MAX_TOKENS` is 32768 (was 16384), sized against the production output-token distribution: 99% of calls finish under 3K tokens, then a tiny tail of runaway-DQI calls hit the cap exactly. The bump is cost-neutral on the 99% path (model emits what it needs, not the cap) and lets the runaway tail complete instead of truncating. **(2)** Terseness-on-retry — when the previous attempt's failure looks like a truncation (`schema_invalid` + `is a required property` + `tokens_out >= 95%` of cap), the next attempt's prompt gets a `MAX 6 markers, MAX 4 sentences for rationale, every required field must be complete` suffix appended. Re-uses the existing `--retry-on-error` budget; zero cost on the success path because terseness is only injected when the heuristic fires. Together the four reliability layers (typography-tolerant matcher + schema-aware salvage + token-cap bump + terseness retry) drop the producer's failure rate from ~12% pre-fix to under ~0.005% expected on full-corpus runs.

Long-tail handling (v0.1): speeches above `--max-words` (default 800) are SKIPPED with reason `text_too_long`. v0.2 will introduce chunked coding with `record_id#chunk-N` keys (mirroring the embedding producer's deferred chunking path). Bootstrap timing on the production corpus: ~$94 cost / ~3 hrs wall-clock at `-j 16` for the recent ~40 months (~19,200 speeches). The `--budget-usd` cap enforces the spend ceiling regardless of which pricing tier OpenRouter actually charges; resume is a no-op skip on every already-coded record.

### `index`

Project `*.extraction.json` sidecars + parallel enrichment files into the live Elasticsearch indices provisioned by `es-init`. The indexer denormalises each sidecar across the nine `mo-*` grains (per Q5 of [`docs/elasticsearch-indexing.md`](docs/elasticsearch-indexing.md)), bulk-upserts via per-grain write aliases, tracks state in `data/monitorul.db` for idempotency, and runs orphan-delete to drop ES docs whose record_ids disappeared between runs (e.g. when a re-extract merges two adjacent speeches into one). Run **after** `extract` / `link` / `backfill` (and any enrichment producers) — the order is `fetch → convert → extract → link → backfill → enrich → index → sitemap`.

```sh
# Index one sidecar
uv run monitorul-ii index pdfs/2018-11-20_MO-PII-168-2018.extraction.json

# Index every sidecar under pdfs/ (idempotent — second run is a no-op)
uv run monitorul-ii index pdfs/

# Dry-run: print per-doc grain counts without contacting ES
uv run monitorul-ii index pdfs/ --dry-run

# Force re-index (ignore idempotency triple)
uv run monitorul-ii index pdfs/ --force

# Restrict projection to one or more grains (repeat --grain to add more)
uv run monitorul-ii index pdfs/ --grain mo-speeches --grain mo-documents

# Blue-green catch-up: write new ingestion to BOTH the live alias AND a
# specific generation while the bulk rebuild runs in parallel.
uv run monitorul-ii index pdfs/today/ \
    --target mo-speeches-20260615-v2 --mirror

# Bootstrap rebuild: full re-index against a fresh generation.
uv run monitorul-ii index pdfs/ --rebuild --target mo-speeches-20260615-v2

# Parallel — threads scale near-linearly on ES round-trips (the bottleneck).
# 20-core box: -j 16 gives a 5-10× speedup.
uv run monitorul-ii index pdfs/ -j 16

# Project the curated persons.json registry into mo-persons too —
# idempotent via a __persons_registry__ sentinel state row.
uv run monitorul-ii index pdfs/ -j 16 --include-persons

# Walk newest→oldest so the most recent stretch is fresh in ES first
# during a long catch-up.
uv run monitorul-ii index pdfs/ -j 16 --reverse

# Capture transient parallel failures to a log file so they don't
# scroll past behind thousands of `ok` lines in the progress bar.
uv run monitorul-ii index pdfs/ -j 16 --errors-log data/index-runs/today.jsonl
```

Flags:

- `--force` — reindex every sidecar regardless of the `(sidecar_content_sha, enrichment_fingerprint, index_generation)` idempotency triple. Useful after schema bumps, mapping changes, or to verify a freshly-cut blue-green generation against the live one.
- `--dry-run` — run the denormalisation + enrichment merge end-to-end without contacting Elasticsearch or writing to the DB. Prints a `dry  <document_id>  [grain=N grain=N ...]` line per sidecar to stdout.
- `--target=<index-name>` — override the write target with a specific generation (e.g. `mo-speeches-20260615-v2`). Only docs whose grain matches the target's prefix are redirected — the rest still flow through their respective live `<grain>-write` aliases. Pair with `--mirror` to keep the live target in sync during blue-green catch-up.
- `--mirror` — when set with `--target`, write to BOTH the target generation AND the live `<grain>-write` alias. The blue-green Q6 catch-up flow.
- `--rebuild` — force a full re-index against `--target` (implies `--force`; the handler errors out with exit 2 if `--target` is missing). Operationally the same as `--force --target=<gen>`; provided as a single-flag convenience for the bootstrap rebuild.
- `--grain=<grain>` — restrict projection to a single grain (or repeat `--grain` to add more). Useful for targeted re-pass after a per-grain mapping bump (`--grain=mo-speeches --force` after the speech analyzer config changes). Accepts any of the nine grain names.
- `--db PATH` — SQLite path for the indexer state table (default `data/monitorul.db`, shared with `fetch`).
- `--index-generation LABEL` — third leg of the idempotency triple (default `live`). Set when running `--target` so the state row tracks the right generation independently of the live one.
- `-j N` / `--workers N` — `ThreadPoolExecutor` worker count (default 1, sequential). Per-sidecar work is network-bound on ES round-trips (bulk + delete_by_query), both of which release the GIL via urllib3, so threads scale near-linearly with worker count up to the cluster's bulk-throughput ceiling. Each worker opens its own `DB(db_path)` connection (SQLite forbids cross-thread sharing); WAL mode handles concurrent reads + serialised writes fine at this rate (one row per sidecar, microseconds per write while ES round-trips are 500 ms+). Output is in **completion order** (not input order) when N > 1; set N=1 for deterministic ordering or single-process debugging. 20-core box: try `-j 16` for a 5–10× speedup. Falls back to the sequential generator path automatically when `N <= 1` or `len(sidecars) == 1`.
- `--include-persons` — also project the curated `persons.json` registry into `mo-persons` after the sidecar loop finishes. Persons aren't sidecar-derived (Q4 of the design doc — they live in `src/monitorul_ii/registries/persons.json`, ~13K curated entries), so the default daily-cron run leaves `mo-persons` alone. Pair with the bootstrap rebuild or after a registry bump (stub merges, Wikidata enrichment). Idempotent: a `__persons_registry__` sentinel row in `es_indexed` stores the registry's content hash; subsequent runs skip until `persons.json` changes. Orphan-delete fires when an entry is removed from the registry, pulling its `/politicieni/<slug>` page out of `mo-persons` so the public site stops serving stale content.
- `--reverse` — process sidecars in reverse order (newest→oldest, since filenames are date-prefixed). A partial run leaves the most recent stretch indexed first — useful when ES is behind on a long catch-up and you want the front page fresh before older history backfills. Applies to the sequential and `-j N` parallel paths alike (parallel output is in completion order regardless, but the dispatch order respects `--reverse`).
- `--errors-log PATH` — append one JSON line per failed sidecar to `PATH` (`{ts, document_id, sidecar_path, errors[]}`). Lazy-create: nothing is written on a clean run. If the flag is omitted **and** any error occurs, the CLI auto-picks `data/index-runs/index-errors-<ISO-timestamp>.jsonl` so failures from a parallel run aren't buried under thousands of interleaved `ok` lines in the rich progress bar (the bar's stderr Console writes both ERR and ok lines, and on a 5,556-doc / 19-min run the 3 ERR lines scroll past the terminal buffer in seconds). The path is surfaced in the final summary (`errors-log=...`) when written. Pass `--errors-log /dev/null` to opt out (rare — most operators want the log).

The indexer transparently retries transient bulk failures: the ES client is configured with `max_retries=3, retry_on_timeout=True, request_timeout=30s` for connection-timeout / network-blip recovery, and `helpers.bulk` is invoked with `max_retries=3, initial_backoff=2, max_backoff=8` so cluster-side `429 es_rejected_execution_exception` (bulk-queue saturation under high `-j N`) auto-retries with exponential backoff. Bulk upserts are idempotent (keyed on `_id`), so the retries are safe. Permanent failures (a malformed sidecar, a mapping conflict) still fail loud and land in the errors-log.

The indexer reads `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS` from the environment (or `.env`); set `ES_API_KEY` to the `monitorul_indexer` key minted by `es-init` for the principle-of-least-privilege production setup. `--dry-run` short-circuits before any client construction so it doesn't need the env vars.

`MONITORUL_ISR_WEBHOOK_URL` opts into the Next.js ISR-invalidation webhook (P5 of the rollout). Until set, the indexer logs the `(grain, record_id)` pairs that would have invalidated; once configured, the live HTTP call fires per upsert.

The state table `es_indexed` records `(document_id → sidecar_content_sha, enrichment_fingerprint, index_generation, indexed_at, child_record_ids)`. The orphan-delete diff compares the previous run's `child_record_ids` against the current run's grouped record_ids and `delete_by_query`'s any orphans, scoped to `document_id` so a misattributed grain can never reach a sibling's records. State rows are versioned by `index_generation` so a major-trigger blue-green flow doesn't collide with the live indexer.

### `query`

Run any of the 10 reference queries from `src/monitorul_ii/elasticsearch/queries.py` against the live `mo-*` indices. The same typed functions back the future Next.js `lib/search.ts` server-side query layer (Q9 of [`docs/elasticsearch-indexing.md`](docs/elasticsearch-indexing.md)) and the LLM-agent's tool wrappers; this CLI is the ad-hoc inspection surface for sanity-checking the index from the command line. See [`docs/elasticsearch-baseline-2026-05.md`](docs/elasticsearch-baseline-2026-05.md) for the canonical 10-query smoke checklist.

```sh
# Search speeches (BM25 over text + agenda_title + speaker.name_search)
uv run monitorul-ii query --name search_speeches --params '{"q":"educație","page_size":5}'

# Look up one MO document by its canonical document_id
uv run monitorul-ii query --name get_document --params '{"document_id":"mo://2018/II/168"}'

# All documents whose session_date matches a specific day
uv run monitorul-ii query --name list_documents_by_date --params '{"date":"2018-11-13","chamber":"Camera Deputaților"}'

# Politician page bundle: person record + recent substantive speeches + query-time stats
uv run monitorul-ii query --name person_page --params '{"person_slug":"iordache-florin"}'

# Terms agg over speaker.party_group_at_time × year — discourse-substrate health check
uv run monitorul-ii query --name agg_speeches_by_party_year --params '{"year":2018}'

# Hybrid search via client-side RRF (BM25 + kNN). Embeds the query
# text via the embed service; falls back to BM25-only if the service
# is down. Runs on basic-license clusters — fusion is computed in
# Python rather than via the Platinum-only `retrievers.rrf` DSL.
uv run monitorul-ii query --name search_speeches \
    --params '{"q":"reformă justiție","rank_fusion":"rrf","page_size":5}'

# Pure kNN ablation — useful for "would the embedding leg alone have
# found the right hit?" research / debugging.
uv run monitorul-ii query --name search_speeches_knn \
    --params '{"q":"educație","page_size":5}'

# Print the request body to stderr before running, alongside the result
uv run monitorul-ii query --name search_speeches --params '{"q":"NATO"}' --explain
```

Flags:

- `--name QUERY` — required. One of: `search_speeches`, `search_speeches_knn`, `list_document_children`, `get_document`, `list_documents_by_date`, `get_agenda_item`, `get_speech`, `person_page`, `search_persons`, `list_committee_meetings`, `get_report`, `agg_speeches_by_party_year`. The function signatures live in `monitorul_ii.elasticsearch.queries`; the CLI dispatches via the `NAMED_QUERIES` registry.
- `--params JSON` — JSON object whose keys map to the query function's keyword arguments. Positional args (`document_id`, `record_id`, `person_slug`, `committee_id`, `q`, `date`) may also be passed via this dict — the CLI promotes them to positional as needed. Default: `{}` (no parameters). Examples: `'{"q":"educație","page_size":5}'`, `'{"record_id":"mo://2018/II/168#agenda-1"}'`.
- `--explain` — print the request body (index + body, JSON-formatted) on stderr before running each ES call, in addition to the result. Useful for debugging the filter / agg shape against the ES query DSL docs. Wraps both `es.search` and `es.get`.

The 12 named queries:

- `search_speeches` — multi_match over speech text + agenda titles + speaker names; `is_substantive: true` default. Filters: `q`, `speaker_person_id`, `chamber`, `document_id`, `date_from`/`to`, `ref_bills`, `topics`. `rank_fusion` accepts `"bm25-only"` (default), `"rrf"` (client-side BM25 + kNN fusion — works on any ES license tier), or `"knn-only"`.
- `search_speeches_knn` — pure kNN ablation query; never falls back to BM25. Returns empty when no vector is available so the embed-service unreachable case is detectable.
- `list_document_children` — multi-index search over every per-doc child grain (agenda-items, speeches, votes, interpellations, questions, committee-meetings) for one `document_id`, sorted by `position_in_document` ASC with `record_id` lex tie-breaker. **Drives the `/mo/<id>` full-document playback page**: returns interleaved hits in true source order across all grains, so the renderer dispatches per-grain via each hit's `index` field. Default `page_size=500` covers every observed doc; paging is available for outliers.
- `get_document` / `get_agenda_item` / `get_speech` / `get_report` — single lookup by canonical `record_id`; 404 returns `null`.
- `list_documents_by_date` — all MOs whose `session_date` matches a given day, sorted by `published` DESC.
- `person_page` — composite `/politicieni/<slug>` payload: person record + 20 most-recent substantive speeches + query-time stats agg (chambers / years / party-group histogram).
- `search_persons` — `multi_match` with `operator: and` over `canonical_name` (text + folded) + `aliases`; multi-token names disambiguate to the right person.
- `list_committee_meetings` — meetings for one committee_id, sorted by `meeting_date` DESC.
- `agg_speeches_by_party_year` — terms agg on `speaker.party_group_at_time` × `year`, with `cardinality(speaker.person_id)` for distinct speakers; the discourse-substrate health check.

The query layer enforces a few server-side guardrails by design (Q9): page sizes are clamped to `MAX_PAGE_SIZE = 50`; `search_speeches` defaults to `is_substantive: true` (chair-procedure turns hidden from public search; flip with `"is_substantive": false` for the admin / discourse-research view); `agg_speeches_by_party_year` always filters to `is_substantive: true`. These are not client-side suggestions — they're correctness properties enforced in `queries.py`. If the webapp or LLM agent needs a wider surface, add a function rather than relaxing the guardrails.

`rank_fusion` on `search_speeches` accepts three values:

- `"bm25-only"` (default) — historical BM25 multi_match path; no embedding required.
- `"rrf"` — **client-side** Reciprocal Rank Fusion over a BM25 leg + a kNN leg on `enrichments.embedding`. ES native `retrievers.rrf` (introduced in 8.9) is gated behind a Platinum+ license — basic-tier clusters return `403 / current license is non-compliant for [Reciprocal Rank Fusion (RRF)]`. To stay portable across license tiers, the query layer issues BM25 + kNN as two separate `_search` calls and fuses them in Python with the same formula (`Σ 1/(RRF_RANK_CONSTANT + rank_in_leg)`). Two ES round-trips per query instead of one; latency penalty is negligible at our QPS, and the fusion math is identical. `result.total` carries the BM25 leg's total (the meaningful "matching documents" count for paging). When `q` is set, the function calls the embed service to vectorise the query text (`EMBED_URL` env var, then default `http://127.0.0.1:8000`); pass a precomputed `query_vector` to skip that hop. If the embed service is unreachable, the function silently degrades to BM25-only — callers don't crash on a missing embedder.
- `"knn-only"` — pure kNN retrieval; useful for ablation / debugging. Requires either a `query_vector` or a reachable embed service. Without a vector, returns an empty result rather than degrading to BM25, so the misconfiguration is detectable.

The dedicated `search_speeches_knn` debug query is the same shape as `search_speeches(rank_fusion="knn-only")` but never falls back to BM25 — its purpose is "did the embedding leg alone find the right hit?" ablation. Listed in `NAMED_QUERIES`; runs via `monitorul-ii query --name search_speeches_knn --params '{"q":"..."}'`. RRF tuning lives in `queries.py`: `RRF_RANK_CONSTANT = 60` (ES default), `RRF_RANK_WINDOW_FLOOR = 100` (per-leg candidate floor; widens to `page * page_size` when paging deep), `RRF_NUM_CANDIDATES_FLOOR = 100` and `RRF_NUM_CANDIDATES_MULT = 10` (HNSW `num_candidates` for the kNN leg, 10× the candidate fetch — the ES recommended ratio for HNSW recall).

The CLI reads `ES_URL` / `ES_API_KEY` / `ES_VERIFY_CERTS` from the environment (or `.env`); set `ES_API_KEY` to the `monitorul_reader` key minted by `es-init` for read-only access. Exit codes: `0` on success, `2` on validation errors (unknown query name, malformed JSON params, missing required positional, missing ES env), `1` on ES connection / runtime errors.

### `verify-playback` (operator tool, `tools/verify_playback.py`)

Data-integrity gate that proves the ES projection plays back faithfully against the source markdown for every doc in the corpus. Reads each MD body + the matching `*.extraction.json` sidecar (and optionally the live ES projection via `list_document_children`) and asserts twelve properties across three layers: hard correctness (positions in `[0, body_len)`, monotonic, dup-free, parent/child count parity, ES↔sidecar record-set parity), per-record content correctness (speech header at speech position, agenda title in body or SUMAR, interpellation questioner near position, vote-open phrase near vote position, qr-doc regnum within question span), and MD↔sidecar provenance (every body speaker-header is claimed by an activity OR by `coverage.claimed_by_policy[]` boilerplate; every sidecar speech text appears at its declared span). Three "expected, not bugs" exemptions are encoded as filters: speech continuations after narrator/vote/procedural events, agenda titles living in SUMAR rather than at the agenda's body position, and the literal `<chair narration>` speaker. See [`docs/architecture.md` § "Playback verification (Phase 4d)"](docs/architecture.md#playback-verification-phase-4d) for the full mechanics.

```sh
# Full corpus, sidecar-vs-MD only (offline; ~70 s with -j 16)
uv run python tools/verify_playback.py pdfs/ -j 16 --no-es \
    --output data/verify-results/$(date +%Y%m%d-%H%M).jsonl

# Single doc, including ES checks (cluster must be reachable)
uv run python tools/verify_playback.py pdfs/2024-01-03_MO-PII-1-2024.md

# Filter to one issue kind (useful for round-N fix loop scoping)
uv run python tools/verify_playback.py pdfs/ -j 16 --no-es \
    --filter-kind=speaker_mismatch

# Print only md_paths of docs with a specific issue kind, one per line —
# pipes into `xargs uv run monitorul-ii extract --force ...` for targeted
# re-extract during the fix loop.
uv run python tools/verify_playback.py pdfs/ -j 16 --no-es \
    --affected-md-paths-for-kind=dropped_turn
```

Flags:

- `paths` (positional, repeatable) — MD file(s) or directories. Directories are globbed `*.md` (non-recursive); passing a `*.extraction.json` path also resolves to its sibling MD.
- `-j N` / `--workers N` — `ProcessPoolExecutor` size. Defaults to `1`. With `-j 16` on a 20-core box the full 5,552-doc corpus runs in ~70 s sidecar-only or ~1.5 min with ES checks. Each worker constructs its own ES client lazily (urllib3 connection pool isn't fork-safe, so threads are not used).
- `--no-es` — skip Layer A ES correctness checks. The verifier still runs Layer B (per-record content) and Layer C (MD↔sidecar provenance) entirely from the on-disk sidecar. Use this for fast iteration during the fix loop, and turn it on (default) for the final acceptance run.
- `--output FILE.jsonl` — mirror the per-doc JSONL output to a file in addition to stdout. The file ends with a `# summary {...}` line carrying the totals so downstream tooling can tail-read the summary without re-aggregating.
- `--filter-kind KIND` — repeatable. Only emit docs whose issues include this kind. Other docs are skipped (no JSONL row). Use to focus on one bug class (e.g. `--filter-kind=speaker_mismatch`) when iterating a fix.
- `--affected-md-paths-for-kind KIND` — print only the MD paths of docs with at least one issue of this kind, one per line, sorted unique. Pipe-friendly into `xargs uv run monitorul-ii extract --force` for targeted re-extract.
- `--affected-docs-for-kind KIND` — same as above but emits canonical `mo://YYYY/PART/ISSUE` document ids instead of MD paths.
- `--limit N` — stop after N docs. Useful for smoke tests; the rest of the corpus is skipped.

Output shape (one JSONL line per doc):

```json
{"doc_id": "mo://2018/II/168", "md_path": "pdfs/...", "issues": [
   {"kind": "speaker_mismatch", "rid": "...", "attributed": "...",
    "in_body": "...", "position": 36948, "head": "..."}
 ], "stats": {"body_len": 49523, "doc_type": "...", "es_total": 113,
              "sidecar_total": 113, "by_grain": {...}, "by_kind": {...}}}
```

Summary line on stderr (`verified=N passed=M with_issues=K total_issues=T elapsed=Ts`).

The verifier is **not** part of the daily fetch → convert → … → index pipeline; it's a data-integrity gate that runs after every extractor / denormalize / indexer change and before the indexer overwrites ES. Tests in `tests/test_tools_verify_playback.py` cover each issue kind on synthetic input plus the three exemptions. See [`data/verify-results/PROGRESS.md`](data/verify-results/PROGRESS.md) for the running fix-loop log and [`data/verify-results/KNOWN-LIMITATIONS.md`](data/verify-results/KNOWN-LIMITATIONS.md) for residual issues that the verifier surfaces but are deferred (chiefly: 3 budget-debate joint-session docs whose pre-amendment-table chair turns aren't claimed by the SUMAR-driven partitioner).

## Progress and interrupts

All long-running subcommands show a live [`rich`](https://github.com/Textualize/rich) progress bar on stderr when stderr is a terminal, and fall back to a periodic plain-text heartbeat in pipes/CI/cron.

- `fetch` — bar tracks days completed; counters show found / downloaded with cumulative MB / failed, plus S3 uploaded / in-bucket / errors. Per-issue events (`ok`, `skip`, `s3+`, `s3=`, errors) and per-day summary lines scroll above the bar without breaking it. Heartbeat fires every 100 days in pipe mode.
- `convert` — bar tracks PDFs completed; counters show converted / skipped / errors, plus S3 uploaded / in-bucket / errors when uploading. Per-PDF event lines scroll above the bar. Heartbeat fires every 50 PDFs in pipe mode.
- `extract` — bar tracks MDs completed; counters show extracted / skipped / errors, the rolling-mean coverage `cov μ=0.999` for the docs that actually extracted this run, and the same S3 trio. Per-MD event lines scroll above the bar (`ok` lines include the doc_type and claimed_pct). Heartbeat fires every 50 MDs in pipe mode.
- `backfill` — bar tracks sidecars completed for the current pass; counters show fill / skip / err and the S3 trio when uploading. Each pass (`issuing_body` / `ministry` / `proposed_by` / `persons`) opens its own bar with the pass label embedded in the description (`[persons] fill=1,524 skip=2 err=0 · s3 up=1,524 …`) so a multi-pass `--kind=all` run is easy to follow. Per-sidecar `ok` / `skip` / `ERROR` lines scroll above the bar. Heartbeat fires every 100 sidecars in pipe mode. The bar plays well with the `-j N` parallel path: results stream in completion order from the worker pool, the bar advances per result, and the description string reads the live shared counters.
- `index` — bar tracks sidecars completed; counters show indexed / skipped (idempotency-triple match) / orphans-deleted / errors. Per-sidecar `ok` lines include a per-grain breakdown (`[documents=1 agenda-items=8 speeches=233 votes=19]`) and append `orphans=N` when the orphan-delete diff fired. Heartbeat fires every 50 sidecars in pipe mode. Sequential by design — each sidecar's work is dominated by ES bulk + delete-by-query round-trips, so process-pool overhead would dwarf the gain.

Stdout (the final summary line) is unaffected by the tty check, so `monitorul-ii ... > log.txt` keeps a clean machine-readable record while you watch the bar interactively.

`Ctrl+C` prints a final `interrupted: ...` summary line with the totals so far and exits **130** — no traceback, no atexit thread-join race. For `fetch`, the DB-backed resume gate means the next run picks up exactly where you stopped. For `convert`, in-flight worker threads finish their current PDF (a few seconds) before the process exits, so partially-written output never lands on disk; queued-but-not-started PDFs are cancelled cleanly. Re-running picks up at the next missing `.md`. For `backfill` running with `-j N`, the parallel path is interrupt-safe with an escape hatch: workers register `SIG_IGN` for SIGINT so the user's Ctrl+C only hits the main process, the executor's `shutdown(wait=False, cancel_futures=True)` returns instantly (avoiding the second-Ctrl+C-during-join traceback), and the cmd-level handler is two-stage — first Ctrl+C prints the summary and returns 130 (workers finish their current sidecar atomically, the multiprocessing atexit join completes within seconds in the steady state); second Ctrl+C calls `os._exit(130)` and terminates immediately. The escape hatch matters when the worker-init phase is long (20 spawn-context workers each loading the ~5 MB `persons.json` and building the matcher's alias index) — without it, a SIGINT-blocked atexit join can keep the user locked for several seconds. Atomic-rename ensures no half-written sidecars on either path; possible orphaned `.part` files reap with `find pdfs/ -name '*.part' -delete`.

## Resume / SQLite audit log

A SQLite database at `data/monitorul.db` (override with `--db PATH`, disable with `--no-db`) records every day's index fetch and every PDF's lifecycle (discovered → downloaded → uploaded). Two tables: `days` and `issues`. See [`docs/architecture.md`](docs/architecture.md) for the schema.

The DB makes resumes cheap. Re-running the same date range:

- skips the index POST for any day already marked `ok` and strictly in the past — *including weekends and holidays that had zero Partea II issues*. This is the killer feature for multi-year backfills: a 26-year resume ticks through ~9,600 days at zero network cost up to the first gap.
- always re-fetches today (publications can land in batches).
- always re-attempts days marked `failed` and any per-issue rows still in `pending`/`failed`.

Override knobs:

- `--force` — re-fetch every day's index, ignore DB.
- `--rescrape-recent N` — re-fetch the last N days regardless of status (default `0`).

Per-PDF skip is still gated on the file existing on disk (and on the bucket via `head_object`). The DB is authoritative for "did we already crawl this day's index"; the filesystem is authoritative for "is the PDF actually here." Each layer owns what it can answer cheaply and correctly.

Hashes (`sha256`), file sizes, S3 ETags, and ISO8601 UTC timestamps land in the DB so the audit log is complete. Existing PDFs from before the DB existed are imported lazily on contact (re-scrape sees the file on disk, hashes it once, records `status='downloaded'`).

## Proxy

Set `PROXY_URL` in `.env` (see `.env.example`) to route all monitoruloficial.ro traffic — both the index endpoint and PDF downloads — through an HTTP/HTTPS proxy:

```
PROXY_URL=http://brd-customer-XXX-zone-YYY:PASSWORD@brd.superproxy.io:33335
```

`--proxy URL` on the CLI overrides whatever is in the env. `--no-proxy` bypasses both.

## S3 / R2 mirror

When `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `S3_BUCKET` are set, every successfully-downloaded PDF is also uploaded to the bucket. Cloudflare R2 is the intended target — point `S3_ENDPOINT` at the R2 endpoint URL and the rest is plain S3 SigV4:

```
S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=auto
S3_BUCKET=monitorul-ii
```

Object keys mirror the local filename (flat layout). Upload is idempotent (`HEAD` first, `PUT` only when missing), so re-runs are cheap. `--bucket NAME` overrides `S3_BUCKET`; `--no-upload` disables the mirror entirely. Startup runs a `head_bucket` check and aborts with exit 2 if the bucket is unreachable or credentials are wrong.

## Elasticsearch

`monitorul-ii es-init` provisions the v1 Elasticsearch surface for the `monitorul.ai` projection layer. Set the following env vars (also accepted via `.env` like the proxy + S3 vars):

```
ES_URL=https://es.example.com:9200
ES_API_KEY=<encoded-bootstrap-api-key>
ES_VERIFY_CERTS=1
```

- `ES_URL` and `ES_API_KEY` are required for any live ES operation. `--dry-run` skips both, so it's safe to use in CI without secrets.
- `ES_API_KEY` is the **bootstrap** key — it needs cluster admin and `manage` on `mo-*` to install templates, create indices, and mint the per-role keys. After `es-init` runs, you'll have two purpose-scoped keys (`monitorul_reader`, `monitorul_indexer`) printed to stdout; switch downstream consumers to those.
- `ES_VERIFY_CERTS` defaults to `1` (true). Accepted values: `1/0`, `true/false`, `yes/no`, `on/off`. Empty / unset keeps the secure default. Set to `0` only for self-signed dev clusters; production must always verify. When `0`, the client also silences the `elastic_transport.SecurityWarning` emitted at construction and the per-request `urllib3.InsecureRequestWarning` (the latter floods parallel-indexer output at `-j N`); both fire loudly under the secure default so a regression that flips `verify_certs` is impossible to miss.

The Elasticsearch projection layer is **not** the system of record — sidecars on disk + S3 are SOT, and ES is rebuildable overnight from them. See [`docs/elasticsearch-indexing.md`](docs/elasticsearch-indexing.md) for the full design rationale (Q1–Q9, including rejected alternatives), and [`docs/architecture.md`](docs/architecture.md) for the operational mechanics of the bootstrap.

### Diacritic-insensitive search

Romanian users frequently type without diacritics — `sosoaca` instead of `șoșoacă`, `educatie` instead of `educație`. Every user-searchable text field (`text`, `agenda_title`, `title`, `topic`, `purpose`, `summary`, `committee_name`, `question_text`, `response.text`, `headings.text`, `agenda_items.title`, `agenda_items.outcome_text`) carries a `.folded` subfield analyzed with the custom `romanian_folded` analyzer (`standard` tokenizer + `lowercase` + `asciifolding` filters). The main field keeps the built-in `romanian` analyzer (preserves diacritics, applies stemming) at a higher boost; the `.folded` subfield is searched in parallel at a lower boost. A query for `sosoaca` matches indexed `șoșoacă` via the folded subfield; a query for `șoșoacă` still ranks the diacritic-correct match first because the main field's higher boost dominates. `mo-persons.canonical_name.folded` already followed this pattern; the v0.16.x mapping change extends it to every other grain.

`SPEECH_SEARCH_FIELDS` in `src/monitorul_ii/elasticsearch/queries.py` is the single source of truth for the field list + boosts that `search_speeches` (and the BM25 leg of RRF) issues. Tuning the boosts is a one-line change there once query logs surface a measurable preference.

Adding `.folded` subfields to existing indices is additive — push via `monitorul-ii es-init --update-mappings`, then run `monitorul-ii index pdfs/ --force` to rewrite every doc so the new subfield populates (ES does not retroactively re-analyze existing docs on a mapping change). The vector-search side (`enrichments.embedding`) is unaffected: BGE-M3 handles diacritic variation natively, indexed embeddings are over the original (diacritic-bearing) text, and queries are passed verbatim to the embed service — folding the query before embedding would push it off-distribution and hurt kNN recall.

The indexer (`monitorul-ii index`) is the routine-trigger codepath from Q6 — daily MO ingestion, re-extraction, linker reruns, backfill reruns, enrichment producer version bumps, new enrichment producers, and redactions all flow through `index_one(...)` calls keyed by `record_id`. Major triggers (mapping changes, schema breaking-changes) cut a new generation via the blue-green helpers in `monitorul_ii.elasticsearch.blue_green` (create target → dual-write via `--mirror` → swap read alias atomically → drop old after cooldown). State tracking lives in `data/monitorul.db`'s `es_indexed` table; the idempotency triple `(sidecar_content_sha, enrichment_fingerprint, index_generation)` is the load-bearing skip key. Enrichment files alongside each sidecar (`<basename>.<producer>.v<version>.json`, `<basename>.<producer>.<model>.v<version>.json`, plus `<basename>.journal.jsonl`) are merged by `record_id` at index time, with a stale-fingerprint filter dropping entries whose `_meta.source_sidecar_content_sha` no longer matches the sidecar — see [`docs/architecture.md`](docs/architecture.md) for the full state-tracking + orphan-delete + blue-green flow.

## Embeddings

The BGE-M3 embedding service lives at [`services/embed/`](services/embed/) and exposes `POST /embed` for the `monitorul-ii embed` producer + the `search_speeches(rank_fusion="rrf"|"knn-only")` query path. Spin it up via the project-root [`docker-compose.yml`](docker-compose.yml):

```sh
# CPU (default; works anywhere)
docker compose up -d
docker compose logs -f embed     # watch model load (~30 s on first run)
curl -fsS http://127.0.0.1:8000/healthz   # smoke

# GPU (requires nvidia-container-toolkit on the host)
TARGET=gpu docker compose build embed
docker compose --profile gpu up -d

# Stop
docker compose down
```

Configuration:

```
EMBED_URL=http://127.0.0.1:8000        # CLI + query layer (default)
EMBED_PORT=8000                         # host port the compose file publishes
EMBED_MODEL_ID=BAAI/bge-m3              # override at build/runtime
EMBED_MAX_BATCH_SIZE=32                 # internal batching ceiling
TARGET=cpu                              # or `gpu` (compose build arg)
```

- `EMBED_URL` is read by both the `embed` subcommand (when `--embed-url` isn't passed) and the `query` subcommand (when `rank_fusion` requires vectorising the query text). Default: `http://127.0.0.1:8000`.
- The named volume `monitorul_hf-cache` keeps the ~2 GB BAAI/bge-m3 weights warm across container rebuilds — first run downloads, every subsequent `up` is instant. Force a re-download with `docker volume rm monitorul_hf-cache` (after a model bump).
- The service is stateless and **not** the system of record — vector files on disk + S3 are SOT. Lose the container → `docker compose up` rebuilds from the Dockerfile; weights re-download on first call. Lose the vector files → re-run `monitorul-ii embed pdfs/ --force`.
- Embeddings are NOT in the main project's dependency closure (see `pyproject.toml` — `httpx` is the only client-side dep). The service ships its own `requirements.txt` with `sentence-transformers` + `torch`. Run it locally for development or remotely on a GPU box for production embedding. The compose stack is intentionally embed-only — Elasticsearch lives outside (`ES_URL` / `ES_API_KEY` in `.env`) per the design doc's "ES is a derived projection, not SOT" stance.

Detached run + smoke:

```sh
docker compose up -d
docker compose ps                                      # `embed` healthy
curl -X POST http://127.0.0.1:8000/embed \
    -H 'Content-Type: application/json' \
    -d '{"texts":["bună ziua, doamnelor și domnilor"]}' \
    | jq '.vectors[0] | length'
# → 1024
uv run monitorul-ii embed pdfs/ --dry-run             # walk records without writing
uv run monitorul-ii embed pdfs/                        # do the real thing
```

## Discourse analysis

The `monitorul-ii analyze` subcommand runs a four-prompt LLM pipeline (Hawkins populism → voice attribution → DQI deliberative quality → V-Party + V-Dem anti-pluralism) over every substantive speech via OpenRouter. Default model: `google/gemini-3.1-flash-lite` (the one picked by the calibration sweeps documented in `docs/discourse-pilot-baseline-2026-05.md`). Outputs persist as `<basename>.discourse.flash-lite.v0_1.json` enrichment files; the indexer flattens the payload onto `mo-speeches.enrichments.discourse.{hawkins,voice,dqi,vparty}.*` automatically (see § `analyze` above for the full prose).

> **Operational runbook**: see [`docs/runbook-analyze.md`](docs/runbook-analyze.md) for the production launch pattern, live observation (progress bar signals, JSONL tail with `jq`, OpenRouter dashboard), Ctrl+C safety contract, resume mechanics, failure-mode triage, `-j N` tuning, and orphaned-process cleanup.
>
> **Retrieval-time reference**: see [`docs/discourse-and-semantic-search.md`](docs/discourse-and-semantic-search.md) for how the discourse fields compose with BGE-M3 semantic search at query time (filters, sort keys, aggregation buckets, the seven canonical query patterns, journalist-UI surfaces, and the four-cell H × V cross-tab analysis pattern).

Configuration:

```
OPENROUTER_API_KEY=sk-or-v1-...           # required for any live analyze run
OPENROUTER_URL=https://openrouter.ai/api/v1   # optional (default)
```

- `OPENROUTER_API_KEY` is read from the environment (or `.env` via python-dotenv). The CLI fails fast (exit 2) when the key is missing.
- `OPENROUTER_URL` is optional and defaults to OpenRouter's production endpoint. Override it for testing against a local proxy or an alternative gateway.
- Cost: at the calibration baseline (~$0.005 / speech across the 4-prompt pipeline), a 40-month backfill of ~19,200 substantive speeches costs ~$94. The `--budget-usd` flag enforces the spend cap; resume after the cap is hit by re-running (fingerprint-match short-circuits at zero cost).
- Pipeline prompts live in `prompts/<framework>_v<N>.{md,schema.json}`; production-pinned versions are `hawkins=v1, voice=v1, dqi=v1, vparty=v2` (the v2 V-Party prompt was rolled out alongside the calibration sweeps; v1 stays in tree for reproducibility).
- The producer's `_meta.errors[]` records per-prompt failures without aborting the sidecar — a Hawkins parse failure, for example, persists the entry with `hawkins=null` and an error string under `_meta.errors`. Re-run with `--force` after fixing the upstream issue.

```sh
uv run monitorul-ii analyze pdfs/ --dry-run                    # plan-only
uv run monitorul-ii analyze pdfs/ --reverse --budget-usd 100   # backfill recent corpus
uv run monitorul-ii analyze pdfs/today/                        # daily-cron flavour
```

## How it works

The site exposes one undocumented AJAX endpoint that returns the day's index:

- `POST https://monitoruloficial.ro/ramo_customs/emonitor/get_mo.php` with body `today=YYYY-MM-DD` returns an HTML fragment containing `<a href="/Monitorul-Oficial--P<part>--<num>--<year>.html">` links per Partea.
- Following any of those `.html` URLs returns the PDF binary directly (`Content-Type: application/pdf`).

Issue numbers can have suffixes (`358Bis`, `12c`). Empty days (weekends, holidays) return zero issues for Partea II.

## Development

Layout:

| File | Role |
|---|---|
| `src/monitorul_ii/scraper.py` | Pure functions: `fetch_index`, `parse_issues`, `download_pdf`, `scrape_day`, `_with_retry`. No CLI concerns. |
| `src/monitorul_ii/converter.py` | Pure functions: `convert_pdf`, `convert_all`, `clean_markdown`, `enrich_meta`. Wraps `pymupdf4llm`. |
| `src/monitorul_ii/classifier.py` | Pure functions: `classify`, `classify_file`, `parse_issue_suffix`, `collect_mds`. Type detector — step 1 of the extraction pipeline. |
| `src/monitorul_ii/extraction/` | Subpackage. `pipeline.py` is the dispatcher (envelope build, coverage compute, schema validate, atomic write); `extractors/<type>.py` is one module per `DocumentType` (plus `extractors/plenary/` sub-subpackage with `agenda.py / activities.py / votes.py / interpellations.py / session.py / boilerplate.py`); `boilerplate.py`, `coverage.py`, `envelope.py`, `references.py`, `schema.py`, `speakers.py`, `topics.py` are shared helpers. Each helper exports its own `*_VERSION` constant; the dispatcher copies them all into each sidecar's `extractor_versions` for selective re-extraction. |
| `src/monitorul_ii/extraction_schema.json` | Canonical JSON Schema for the sidecar shape (loaded at module import, validated pre-write). Mirrors `docs/extraction-schema.md`. |
| `src/monitorul_ii/elasticsearch/` | Subpackage. `config.py` is `ESConfig.from_env()`; `client.py` builds an `Elasticsearch` 8.x client from the config; `bootstrap.py` provisions component templates, index templates, indices, aliases, and API keys (idempotent); `mappings/` holds one JSON per grain (the source of truth for v1 field shapes). See `docs/elasticsearch-indexing.md` for the design rationale. |
| `src/monitorul_ii/uploader.py` | `S3Config.from_env()` + `Uploader` (boto3, S3-compatible incl. R2). |
| `src/monitorul_ii/db.py` | `DB` — thin SQLite wrapper over `days` + `issues` tables; owns the resume-gate logic. |
| `src/monitorul_ii/cli.py` | argparse, exit codes, the live progress bar / heartbeat, the upload→DB write path. |

`docs/architecture.md` is the deep dive — the site contract, link parser, retry policy, schema, resume contract, and the markdown-cleanup pipeline.

### Setup

```sh
uv sync                          # creates .venv and installs all deps
cp .env.example .env             # fill in PROXY_URL and S3_* vars as needed
```

The CLI is the entry point in `pyproject.toml`. You can also invoke it as a module:

```sh
uv run python -m monitorul_ii fetch 2026-04-29
```

### Lint and format

```sh
uv run ruff check                # lint (--fix to auto-fix)
uv run ruff format               # format
```

No committed `ruff` config — defaults apply. Run both before committing.

### Tests

```sh
uv run pytest
```

Tests live in `tests/` and mirror `src/monitorul_ii/` (`test_<module>.py`). The suite is fast (~0.4 s) and offline: the scraper is driven through `httpx.MockTransport`, `convert_pdf` is monkeypatched away from `pymupdf4llm`, the SQLite audit log runs in `tmp_path`, and S3 calls are unit-tested via `S3Config.from_env` only — no real bucket touched.

New features must ship with tests. The contract is:

- New pure function → happy-path + rejection / edge case.
- New CLI flag → one test exercising the branch it toggles.
- New regex / parser branch → one positive, one negative, one quirk sample.
- New DB state transition → drive the transition and assert the row, plus an idempotency test if the transition is re-entrant.
- New scraper / converter behavior → drive `scrape_day` / `convert_all` end-to-end, not just the leaf.

### `uv` on snap quirk

`uv` installed via snap buffers stdout when there is no tty, so `uv run <cmd>` may appear silent in non-interactive shells (including hooks and scripts). Pipe through `cat` (e.g. `uv run monitorul-ii --help | cat`) or invoke the venv binary directly (`.venv/bin/monitorul-ii ...`) when you need to see output.

### Releases

Automated via [release-please](https://github.com/googleapis/release-please), triggered on every push to `main` (`.github/workflows/release-please.yml`). The workflow opens a release PR that bumps the version and updates the changelog; merging it tags and publishes.

- Commits **must** follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:`, `docs:`, …) for release-please to pick them up. Anything outside that grammar is ignored — no version bump, no changelog entry.
- Pre-1.0: `feat:` bumps the minor; `fix:` bumps the patch.
- Tags include the component name (`monitorul-ii-vX.Y.Z`).
- The version source of truth is `.release-please-manifest.json`, **not** `pyproject.toml` — keep them in sync if you ever bump by hand.
