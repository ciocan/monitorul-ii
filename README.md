# monitorul-ii

Scrape [Monitorul Oficial al României](https://monitoruloficial.ro/e-monitor/) Partea a II-a (and other parts), save the PDFs locally, and convert them to extraction-friendly markdown.

## Install

```sh
uv sync
```

## Usage

Four subcommands: `fetch` (download PDFs), `convert` (PDF → markdown), `classify` (type-detect MDs into the extraction-schema buckets), and `extract` (MD → structured JSON sidecar).

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

When S3 env vars are set, modified sidecars re-upload (overwriting the bucket copy) so the bucket stays in sync with the local files. `--no-upload` disables the mirror.

All three passes are idempotent: re-extracting a sidecar (extractor version bump → re-extract) clobbers linker-written fields, but a quick `monitorul-ii link` recovers. The cross-doc passes are fast (~1ms per doc — pure dict lookup); the xref pass is also fast (single walk per sidecar, no MD body access). On the 5,551-doc corpus, a second run yields zero new resolutions — idempotency is corpus-verified for the xref pass.

### `backfill`

Registry-driven backfill — fills the schema's `*_normalized` slots by joining curated registries against the raw values that `extract` already pulled. Same architectural shape as `link`: read sidecars, look up each raw value in an in-memory registry index, write the canonical id back atomically with pre-write schema validation. Each `--kind` targets one registry/field pair.

```sh
# run every shipped backfill pass over a directory
uv run monitorul-ii backfill pdfs/

# only the institutional-bodies pass (report_facsimile.issuing_body_normalized)
uv run monitorul-ii backfill pdfs/ --kind=issuing_body

# preview without writing anything to disk or S3
uv run monitorul-ii backfill pdfs/ --dry-run

# overwrite existing *_normalized values when the registry now resolves a different id
uv run monitorul-ii backfill pdfs/ --force

# skip the S3 mirror (otherwise modified sidecars re-upload)
uv run monitorul-ii backfill pdfs/ --no-upload
```

`--kind` selects which pass to run; choices are `issuing_body` (fills `report_facsimile.body.report.issuing_body_normalized` from the institutional-bodies registry), `ministry` (fills `question_register.body.questions[].addressee.ministry_normalized` and `plenary_*.body.interpellations[].addressed_to_normalized` from the ministries registry, with institutional-body fallback for non-ministry addressees like `Curtea de Conturi`), `proposed_by` (fills `plenary_*.body.agenda_items[].activities[].proposed_by` with the canonical `Guvern` Speaker on votes whose parent agenda carries an OUG/OG cite — Government Ordinances are by definition government-proposed), and `all` (default — runs every shipped pass; forward-compatible with the future person registry). Unknown choices are rejected by argparse — `--kind=person` exits with usage info until the person registry ships. `--force` overwrites `*_normalized` values that disagree with the registry's current canonical id; without `--force`, conflicts skip with reason. `--dry-run` reports planned changes (one line per sidecar showing the raw value, the canonical id, and the matched-via tier) and exits without writing.

**Institutional-bodies registry** (30 entries: CSAT, SRI, SIE, STS, BNR, ICR, Avocatul Poporului, Consiliul Legislativ, SRTv, SRR, ANCOM, ANRE, Curtea de Conturi, Curtea Constituțională, ANI, ASF, ANSPDCP, CSM, ONPCSB, AGERPRES, ANAD, AEP, ICCJ, CCIR, CNA, CNSAS, CNCD, ANRM, ANCPI, SPP). Production smoke on the 52 R-suffix corpus (post `report_facsimile.py` v0.2.0): 52/52 (100%) of R-suffix sidecars resolve to a canonical id — the v0.1.0 18-doc gap (mostly ANCOM `pentru anul`, ANRE `<br>` linebreak residue, AEP election-day `din [DD] month YYYY` forms) closed by the extractor's surface-form expansion.

**Ministries registry** (30 entries — one id per "ministry concept" with historical names + Romanian genitive declensions as aliases: prime_minister, health, culture, education, research, transport, environment, finance, foreign_affairs, economy, energy, regional_dev, youth_sport, communications, labor, justice, eu_funds, tourism, family, internal_affairs, defense, romanians_abroad, agriculture, sme, infrastructure_projects, delegated_water, delegated_budget, delegated_higher_education, secretariat_general, secretariat_revolutionaries). Production smoke (post `interpellations.py` v0.2.3 `addressed_to` rewrite): qr questions resolve at **92.7%** (1851/1997 non-null raws); plenary interpellations resolve at **89.4%** (1849/2068 non-null raws — was 25% pre-v0.2.3, before the upstream extractor was fixed to capture full ministry / role-form / PM strings instead of single-letter noise).

**proposed_by pass** is signal-driven, not registry-driven: there's no curated table of bill sponsors (the Tier 4 prompt scoped that to ~10K bills, requiring per-bill metadata that lives on parlament.ro). Instead, the pass attributes votes to the Government when the parent agenda carries an `oug` / `og` reference OR the title matches an OUG/OG cite pattern (`OUG nr. X/Y`, `Ordonanței Guvernului nr. X/Y`, `O.U.G.`, `O.G.`). Government Ordinances are by definition government-issued, and any vote on a bill approving one inherits that proposer. Production smoke: 10.76% of plenary votes (5330/49556) attributed to Guvern; 554 of 4446 plenary sidecars touched (post-`agenda.py` v0.2.5 re-baseline; the previous figure of 14.2% / 7018 votes was inflated by misattributed OUG/OG cites in contaminated agenda titles, which the title-contamination clip now removes). The remaining ~89% are PL-x / L bills proposed by parliamentary groups / individual MPs / committees — that long tail requires a parlament.ro per-bill scrape and is deferred (see `docs/architecture.md` § Future graduation candidates).

Match strategy is exact → case-insensitive → diacritic-stripped (cedilla `ţ`/`ş` collapses to comma `ț`/`ș`; mojibake replacement chars `�` strip out) → token-set → **prefix** (ministries only — last-resort longest-prefix match, where the cleaned input starts with a registered alias at a whitespace boundary; recovers from the plenary extractor's habit of bleeding sentence prose into `addressed_to`, without admitting fuzzy matches). No fuzzy / Levenshtein tier (precision over recall — the registry must NEVER misclassify a body).

Backfill versions are NOT part of `extractor_versions` — re-extracting a sidecar clobbers backfill-written fields. Re-running `backfill` after `extract` recovers them; backfill is fast (in-memory dict lookup per record).

## Progress and interrupts

Both subcommands show a live [`rich`](https://github.com/Textualize/rich) progress bar on stderr when stderr is a terminal, and fall back to a periodic plain-text heartbeat in pipes/CI/cron.

- `fetch` — bar tracks days completed; counters show found / downloaded with cumulative MB / failed, plus S3 uploaded / in-bucket / errors. Per-issue events (`ok`, `skip`, `s3+`, `s3=`, errors) and per-day summary lines scroll above the bar without breaking it. Heartbeat fires every 100 days in pipe mode.
- `convert` — bar tracks PDFs completed; counters show converted / skipped / errors, plus S3 uploaded / in-bucket / errors when uploading. Per-PDF event lines scroll above the bar. Heartbeat fires every 50 PDFs in pipe mode.
- `extract` — bar tracks MDs completed; counters show extracted / skipped / errors, the rolling-mean coverage `cov μ=0.999` for the docs that actually extracted this run, and the same S3 trio. Per-MD event lines scroll above the bar (`ok` lines include the doc_type and claimed_pct). Heartbeat fires every 50 MDs in pipe mode.

Stdout (the final summary line) is unaffected by the tty check, so `monitorul-ii ... > log.txt` keeps a clean machine-readable record while you watch the bar interactively.

`Ctrl+C` prints a final `interrupted: ...` summary line with the totals so far and exits **130** — no traceback, no atexit thread-join race. For `fetch`, the DB-backed resume gate means the next run picks up exactly where you stopped. For `convert`, in-flight worker threads finish their current PDF (a few seconds) before the process exits, so partially-written output never lands on disk; queued-but-not-started PDFs are cancelled cleanly. Re-running picks up at the next missing `.md`.

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
