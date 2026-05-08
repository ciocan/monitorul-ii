# Canonical queries — version-pinned rankings and searches over the discourse-analysis layer

Companion to [`discourse-analysis-schema.md`](./discourse-analysis-schema.md). The schema doc records the *per-speech* substrate: what is coded, under which framework, with what evidence, and at what voice. **This file pins the *aggregations* on top of that substrate** — the named, version-locked query patterns the project will publish.

Every published finding cites a query name and version (e.g. `most_populist_politicians@v1`); the spec for that name lives here. Adding a new query, or changing a filter rule, is a versioned change to this file — same discipline the canonical and discourse-analysis schemas apply to their own field shapes.

## Why pin queries before code

Without a frozen query catalogue, the corpus's findings become uniteable: every news article publishes a different ranking, every researcher disagrees on inclusion thresholds, and the project's value as research infrastructure collapses. Two specific failure modes pinning prevents:

1. **Inclusion-threshold gerrymandering.** "Most populist senator in 2020" depends on what counts as a codable speech, what minimum word count qualifies, and how few speeches a senator can give before being excluded. Different choices produce different headline politicians from the same data. Without pinning, every publication picks its own thresholds (often unconsciously) and the corpus loses interpretability.
2. **Methodology-drift accusations.** When a politician contests a finding, "we used the project's documented `most_populist_politicians@v1` query, here is the spec, here are the evidenced markers" is a defensible posture. "We ranked them by populism" without a versioned spec is not.

Pinning is also the cheapest, highest-leverage methodology investment in the whole layer: the queries doc is text, it doesn't change the schema, it doesn't bloat any sidecar, and it makes every published finding reproducible and citable. Q7 of the discourse-analysis-schema doc names this as the keystone aggregation decision; this file is the operationalisation.

## How to read this doc

Each **query is a frozen contract**. Once a query ships at `@v1`, its inputs, filters, scoring formula, output shape, and disclaimer text are immutable. Refining a filter rule, adding a confidence threshold, switching the normalisation basis — any of these is a major-version bump (`@v2`), and the prior version stays defined here permanently so old publications remain reproducible.

The queries are organised by *tier* — what kind of question they answer — not alphabetically. Tier A covers ranked rhetorical-pathology queries (what the journalist-facing prototype publishes first). Tier B covers the positive deliberative-quality axis. Tier C covers cross-axis divergence (the "left politician, right-wing speech" question class). Tier D is registry-powered party-level queries. Tier E is audience-reception queries (zero-LLM, regex-only, ships first per the build order). Tier F is generic search queries (filter, return matches with evidence, no aggregation).

The conventions section codifies what every query does *by default*; per-query specs only record diffs from the default. This is deliberate — keeping the default in one place means a methodology improvement applied across the catalogue is one edit, not fourteen.

## Conventions

Applied to every query unless overridden in the query's own spec. The conventions are themselves versioned (`conventions@v1`); a major bump propagates to every query that does not explicitly pin a prior conventions version.

### Time-window granularities

Three default granularities; each query's `time_window` input is one of:

- **Calendar year** (e.g. `2020`). Matches journalist phrasing — *"most populist senator in 2020"*.
- **Per legislature** (e.g. `legislature-IX`). Matches political-science convention; legislatures are Roman-numeralled in canonical metadata.
- **Rolling 12-month** (e.g. `2024-03-01 → 2025-02-28`). Smooth view; trend lines.

Per-session (~6 months) is too short and noisy to be a default granularity; queries can specify it as a custom input but it does not appear in standard outputs. Custom windows (`from`, `to`) are accepted on every query but the disclaimer flags them as non-standard.

### Speech-eligibility filter (default)

A speech enters a query's substrate only if **all** of the following hold. Per-query specs may add filters but should not relax these without a `@v2` bump.

- `length_words >= 100` — strips chair-procedure cues, one-line interjections, and floor-time grabs that distort per-speech-rate aggregations. **Stricter than** the index-time `is_substantive` flag, which gates on `text_length >= 100` *characters* (≈15–20 words at Romanian word lengths). The discourse-analysis layer needs the higher word-count bar because per-speech rate aggregations of populism, polarization, etc. become unstable on short turns even if they clear the chairs-procedure cutoff. Two different gates, same round number.
- `speaker.party_group` resolves to a registered alias in `registries/party_positions.json` — guarantees the speech is attributable to a party for join-time queries. Speeches by guests, ministerial witnesses, and unattributed chair narration drop out.
- `delivery_mode == "delivered"` — written interpellations (`întrebări scrise`) and inserted-for-record items follow different rhetorical norms; queries that intentionally include them flag the override (`include_written: true`) and the disclaimer notes the inclusion.
- `coverage.claimed_pct >= 0.85` on the parent canonical document — rejects under-extracted documents whose per-speech substrate is unreliable. Documents below the threshold appear in the `extraction-baseline` reports and are eligible for re-extraction; until then their codings are excluded from rankings.
- `skipped_short == false` — the analysis-layer `--min-words` floor (default 50) screens out speeches the producer chose not to code. This filter and `length_words >= 100` are independent: `--min-words` is a *cost* gate at coding time; `length_words >= 100` is an *eligibility* gate at query time. The two thresholds intentionally differ so a configurable producer floor never silently changes published query semantics.

### Politician-eligibility filter (default for ranked outputs)

A politician must clear **both** in the query's window:

- `>= 10 codable speeches` (after speech-eligibility filter).
- `>= 5,000 words` (sum of `length_words` across codable speeches).

Politicians below threshold are reported in a parallel **low-evidence tier** with their actual counts, never in the headline ranking. Without the threshold, politicians with one or two speeches dominate the top of "% populist" rankings by accident — a single highly-populist intervention puts them at 100%. (Hawkins's published-paper convention for the Global Populism Database is similar: leaders need a minimum number of speeches before a populism score is published.)

### Coding-eligibility filter (default for primary ranking)

- `framework_confidence > 0.7` — the framework-layer self-reported confidence in the coding.
- `voice == "speaker_first_person"` — the keystone defamation-safety filter (Q5 of the discourse-analysis schema). Without it, the ranking mixes populism-deniers with populism-users; the deniers, who quote populist tropes more often than they utter their own (in order to denounce them), rank higher for populism than the actual populists.

Both filters are relaxable for parallel rankings (see below) but never for the primary headline.

### Normalisation basis

Default: **per-1000-words rate** (`markers / kwords`) as the primary score; **per-speech rate** (`markers / speeches`) as the secondary, reported alongside. Per-floor-time rate (`markers / minutes_speaking`) is the cleanest accountability proxy but requires speech-duration metadata that the canonical does not yet carry; deferred to a future conventions bump.

The two bases produce different rankings: per-speech penalises long careers; per-1000-words is closer to "discourse density." Reporting both side-by-side prevents either ranking from being read as the single truth.

### Aggregation function (within a politician × time window)

Always report the following three aggregations side by side; never collapse to one number:

- **Mean** of per-speech scores — sensitive to one-off extreme speeches.
- **75th percentile** — captures *how extreme* the politician's most extreme speeches are; useful for episodic patterns like fearmongering.
- **Sum of marker counts** (after normalisation) — intensity-weighted volume.

A politician who gives 10 wildly-populist speeches and 90 boring ones ranks differently under mean vs 75th percentile, and the difference is journalistically meaningful. The headline ranks by per-1000-words rate of mean-aggregated scores; the other aggregations populate per-row columns in the output.

### Stability — bootstrap confidence intervals on rank

Every ranking carries a **rank 95% CI computed via 1,000-iteration bootstrap** over the speech-set per politician. A politician at rank 3 with 12 speeches and another at rank 4 with 200 speeches are not equivalent; the CI captures the difference. A politician whose 95% CI for rank is `[2, 14]` is not "rank 3"; they are "somewhere in the top-15 cluster."

This single discipline prevents the most common journalism-misuse pattern of treating LLM-coded rankings as more precise than they are. Rankings without rank-CIs are treated as 0.0001-precision; rankings with rank-CIs are treated as the cluster they actually identify.

### Parallel rankings (mandatory, every ranked query)

Three parallel rankings ship alongside every primary ranking. Conflating any of them with the primary produces flattering artifacts for specific operator types; reporting them side by side is the keystone honesty discipline.

1. **High-confidence (`framework_confidence > 0.7 AND voice_confidence > 0.7`) vs all-codings.** Including low-confidence codings makes rankings noisier; excluding them biases toward politicians whose rhetoric is *easier to classify* — LLMs are more confident on stereotypical populism than on subtle dog-whistles, so subtle-rhetoric politicians get under-counted in confidence-filtered rankings. Movements between the two rankings are themselves journalistically meaningful.
2. **First-person (`voice == speaker_first_person`) vs deniable-wrapper.** From Q5: voice is persisted per marker. They are different rhetorical strategies; a politician high on first-person populism is open about it, a politician high on deniable-wrapper rhetoric operates with plausible deniability. Conflating them produces ranking artifacts that consistently flatter the more cunning operators. The deniable-wrapper ranking aggregates over the `rhetorical_moves` bucket (apophasis, weasel-attribution, just-asking-questions, concern-trolling, etc.).
3. **Low-evidence tier.** Politicians below the inclusion threshold, with their actual speech and word counts. Always reported, never promoted to the headline; lets readers see the politicians the threshold excluded and decide whether the threshold is appropriate for their question.

### Cross-time party-affiliation handling

Politicians switch parties; speeches stay timestamped. A politician PSD-in-2018 → independent-in-2020 → splinter-in-2024 contributes:

- Speeches from each window to the corresponding party's per-party rankings.
- A unified politician-level entry for politician-level rankings (joined via `person_id`, never via `party_group`).

The party-position-registry join uses **the speech-date party affiliation**, never "current party." A speech from 2018-03-15 joins PSD's CMP-2016 / CHES-2014 codings (the active manifesto and party programme during the parliamentary mandate that produced that speech), not PSD's 2024 codings. Q6 of the discourse-analysis schema documents the rationale.

### Speech-deduplication

Speeches occasionally appear in multiple canonical documents (a written interpellation later read aloud; a procedural quotation embedded in a later debate). Dedup by `source_span.content_sha` before any aggregation; the first canonical occurrence wins.

### Output schema (every ranking query produces this shape)

```json
{
  "query": "most_populist_politicians",
  "query_version": "v1",
  "conventions_version": "v1",
  "produced_at": "2026-05-04T10:30:00Z",
  "inputs": {
    "time_window": "2020",
    "chamber": "both"
  },
  "rubric_versions_used": {
    "hawkins_populism": { "rubric_version": "hawkins@2018", "prompt_version": "hawkins_v1", "model": "claude-opus-4-7" }
  },
  "speeches_in_substrate": 4271,
  "politicians_in_substrate": 287,
  "politicians_passing_threshold": 134,
  "primary_ranking": [
    {
      "politician_id": "georgescu-calin",
      "politician_canonical_name": "Călin Georgescu",
      "rank": 1,
      "rank_ci_95": [1, 2],
      "score_per_1000_words_mean": 12.8,
      "score_per_speech_mean": 1.42,
      "score_75th_percentile": 2.0,
      "score_sum": 178,
      "speeches_in_window": 41,
      "words_in_window": 24108
    }
  ],
  "parallel_rankings": {
    "deniable_wrapper": [ /* same shape, different substrate */ ],
    "high_confidence_only": [ /* same shape */ ],
    "all_codings": [ /* same shape, no confidence filter */ ]
  },
  "low_evidence_tier": [
    { "politician_id": "...", "speeches_in_window": 4, "words_in_window": 2031 }
  ],
  "methodology_disclaimer": "...",
  "model_disclaimer": "..."
}
```

The `parallel_rankings` block always carries `deniable_wrapper` and `high_confidence_only`; a query may add others if its spec defines them, but never drops these two.

## Disclaimers — mandatory on every ranking output

Two disclaimers, both required, both rendered into every ranking object's payload (not stored separately, not optional). Pinned here so that the prototype can never gradually shed them as it matures.

### Methodology disclaimer (rendered verbatim into `methodology_disclaimer`)

> "This ranking is derived from machine-coded discourse analysis under the published [framework_name] rubric ([rubric_version]). Codings have not been validated against human inter-coder agreement (κ); the project plans to publish κ statistics in a future release. Treat this ranking as exploratory and subject to revision under future methodology. Per-speech evidence — the markers and source spans behind every coding — is available in the underlying analysis sidecars and should be consulted when any individual ranking is contested."

### Model disclaimer (rendered verbatim into `model_disclaimer`)

> "Codings produced by [model_id] under prompt version [prompt_version] and rubric version [rubric_version]. Recoding under different model, prompt, or rubric configurations may produce different rankings. Model snapshots and prompt versions are pinned per-coding in the analysis sidecars; this ranking aggregates only codings whose `(model, prompt, rubric)` triple matches the configuration named above."

### When the κ track ships

The methodology disclaimer's first sentence changes from *"Codings have not been validated …"* to *"Codings have been validated against human inter-coder agreement at κ = [value] for [framework_name] (see `validation/agreement_report.md`)."* This is a `conventions@v2` change; the old disclaimer remains valid for `conventions@v1` rankings produced before the κ track shipped.

### Implementation note (non-normative)

The two disclaimers are *not* a UI concern. They live inside the JSON payload of every query result so any consumer — public dashboard, CLI report, journalist's spreadsheet, downstream API — surfaces them. Strip-disclaimer is grounds for retraction; the schema validator on the report-generator should reject any output object missing either field.

---

## Query catalogue

Each query specifies: **name**, **version**, **description**, **inputs**, **filters** (diffs from default), **score** (formula and substrate), **output diffs** (extra columns or parallel rankings beyond the default), **dependencies** (which frameworks must be coded for this query to run), **disclaimer overrides** (rare; only when the standard two need extension), and **build-order tier** (when in the schema's build order this query becomes runnable).

### Tier A — ranked rhetorical-pathology queries

#### `most_populist_politicians@v1`

**Description.** Rank politicians by Hawkins-graded populism density across delivered floor speeches in the time window.

**Inputs.**
- `time_window` (calendar year | legislature | rolling-12mo | custom).
- `chamber` (`camera | senate | both`; default `both`).
- `min_speeches`, `min_words` (override politician-eligibility defaults; rare).

**Filters.** All defaults apply. Adds:
- `framework == "hawkins_populism"`.

**Score.**
- Substrate: per-speech `hawkins_populism.score` (0/1/2 ordinal).
- Primary: **per-1000-words rate of `(hawkins_populism.score >= 1)` speeches** (the share-of-discourse-density framing Hawkins's published rankings use).
- Secondary: per-speech mean of the ordinal score; 75th percentile; sum of populism markers.

**Dependencies.** `hawkins_populism` framework; voice classifier; persons registry; party-position registry alias resolution.

**Build-order tier.** Runnable once Hawkins ships (build-order step 7 in the schema doc). Among the very first publishable rankings.

**Notes.** This is the canonical ranking the journalist-facing prototype centres on. Cross-validate aggregate-level (per party-cycle) against Global Populism Database codings for Romanian leaders where overlap exists.

---

#### `most_anti_pluralist_politicians@v1`

**Description.** Rank politicians by V-Party anti-pluralism density across delivered floor speeches in the time window. Anti-pluralism captures rhetorical hostility to the conditions of multi-party democracy: opposition delegitimisation, media hostility, judiciary attacks, minority scapegoating, rejection of democratic norms.

**Inputs.** Same as `most_populist_politicians`.

**Filters.** All defaults. Adds:
- `framework == "vparty_anti_pluralism"`.

**Score.**
- Substrate: `vparty_anti_pluralism.score` (continuous 0–1) and the count of fired V-Party markers.
- Primary: **per-1000-words rate of speeches with at least one V-Party marker** (`opposition_delegitimization`, `media_hostility`, `judiciary_attack`, `minority_scapegoating`, `democratic_norms_rejection`).
- Secondary: mean V-Party score per speech (continuous); 75th percentile; sum of markers.

**Output diffs.** Per-marker breakdown column: count of each of the five marker kinds per politician. (V-Party is multi-target; readers want to know whether a high score is driven by judiciary attacks or media hostility — these are different rhetorical patterns.)

**Dependencies.** `vparty_anti_pluralism` framework; voice classifier; persons registry; party-position registry.

**Build-order tier.** Runnable after V-Party ships (build-order step 10).

---

#### `most_fearmongering@v1`

**Description.** Rank politicians by density of fearmongering markers (existential threat, civilisational decline, invasion, demographic replacement, economic collapse, cultural extinction, enemy within) across delivered floor speeches in the time window.

**Inputs.** Same as `most_populist_politicians`.

**Filters.** All defaults. Adds:
- `custom_codings.fearmongering` populated.

**Score.**
- Substrate: `custom_codings.fearmongering.markers[]` filtered to `voice == speaker_first_person`.
- Primary: **per-1000-words rate of fearmongering markers**.
- Secondary: per-speech mean marker count; 75th percentile; sum.

**Output diffs.** Per-marker-kind breakdown column (which of the seven marker kinds dominate this politician's score).

**Dependencies.** `fearmongering` custom rubric; voice classifier; persons registry.

**Build-order tier.** Runnable once the `fearmongering` custom rubric ships (build-order step 14).

**Notes.** Custom rubric — defensibility caveat in the methodology disclaimer is even more important here than for framework-anchored axes. The disclaimer's reference to "the published [framework_name] rubric" reads "the project's custom-fearmongering rubric ([rubric_version])" for this query; the implementation substitutes the framework name string accordingly.

---

#### `rhetorical_fallacy_density@v1`

**Description.** Rank politicians by density of rhetorical-fallacy markers (false dichotomy, strawman, ad hominem, equivocation, hasty generalisation, whataboutism, appeal to emotion, slippery slope, loaded question, red herring, circular reasoning) across delivered floor speeches in the time window.

**Inputs.** Same as `most_populist_politicians`. Adds:
- `fallacy_kinds` (array, optional): if non-empty, restricts the substrate to the named fallacy kinds — e.g. `["ad_hominem", "whataboutism"]` for an "insult and deflection" sub-ranking.

**Filters.** All defaults. Adds:
- `custom_codings.fallacies` populated.

**Score.**
- Substrate: `custom_codings.fallacies.markers[]` filtered to `voice == speaker_first_person`.
- Primary: **per-1000-words rate of fallacy markers** across all 11 fallacy kinds (or the restricted subset when `fallacy_kinds` is specified).
- Secondary: per-fallacy-kind rate for the eleven kinds.

**Output diffs.** Per-fallacy-kind table per politician. (Some fallacies are journalist-friendly; some are more academic. Readers self-select.)

**Dependencies.** `fallacies` custom rubric; voice classifier; persons registry.

**Build-order tier.** Runnable once the `fallacies` custom rubric ships (build-order step 14).

**Notes.** Best journalist-facing proxy for "rhetorical dishonesty" (Q3 of the schema doc rejected a stored "lies" axis for ground-truth reasons; this query is the textual substitute). The disclaimer should note that a high fallacy-density score reflects rhetorical *form*, not *truth-value* of any individual claim.

---

#### `most_securitizing@v1`

**Description.** Rank politicians by density of completed securitisation moves — speeches that frame an issue as existential to demand extraordinary or extra-constitutional measures (Copenhagen-School speech-act). The completed move requires co-occurrence of three markers within the speech: existential framing → referent object named → extraordinary measures invoked.

**Inputs.**
- `time_window`, `chamber`, `min_speeches`, `min_words` (defaults).
- `referent_object_filter` (optional): array of referent kinds (`nation`, `civilisation`, `family`, `sovereignty`, `ethnic_group`) — restricts the substrate to securitisation moves around the named referent. Empty means all.

**Filters.** All defaults. Adds:
- `custom_codings.securitization.securitization_move_present == true`.

**Score.**
- Substrate: speeches where the completed move is present, with their constituent markers.
- Primary: **per-1000-words rate of completed securitisation moves**.
- Secondary: per-speech mean (binary aggregated); per-referent-object breakdown.

**Output diffs.** Per-referent-object breakdown column (civilisation, sovereignty, ethnic_group most likely the dominant ones in the Romanian corpus).

**Dependencies.** `securitization` custom rubric; voice classifier; persons registry.

**Build-order tier.** Runnable once the `securitization` rubric ships (build-order step 13).

**Notes.** Distinct from `most_fearmongering@v1` — fearmongering captures *threat construction*; securitisation captures the speech-act of *demanding extra-constitutional response*. A speech can fearmonger without securitising; a speech can securitise without naming any ethnic enemy. Reporting both rankings side by side is encouraged in any "rising authoritarianism" feature.

---

#### `most_conspiracy_framing@v1`

**Description.** Rank politicians by density of conspiracy-framing markers (`parallel_state` / `statul paralel`, Soros conspiracy, globalist cabal, deep state, cultural Marxism, "they don't want you to know", false-flag accusation, hidden hand, replacement theory, medical conspiracy) across delivered floor speeches in the time window.

**Inputs.** Same as `most_populist_politicians`. Adds:
- `marker_kinds` (array, optional): restrict to a named subset of the conspiracy markers.

**Filters.** All defaults. Adds:
- `custom_codings.conspiracy_framing.markers[]` populated.

**Score.**
- Substrate: `custom_codings.conspiracy_framing.markers[]` filtered to `voice == speaker_first_person` and (if specified) `marker_kinds`.
- Primary: **per-1000-words rate of conspiracy markers**.
- Secondary: per-speech mean count; 75th percentile; sum.

**Output diffs.** Per-marker-kind breakdown column. Era-window flag column: any marker appearing outside its `era_window` (e.g. `parallel_state` before 2017) gets flagged in the politician's row, since these are likely false positives.

**Dependencies.** `conspiracy_framing` custom rubric; voice classifier; persons registry.

**Build-order tier.** Runnable once the `conspiracy_framing` rubric ships (build-order step 12 — high Romanian-context value, scheduled before the other custom buckets).

**Notes.** The Romanian corpus has corpus-specific salience here (`statul paralel`, Soros, globalist cabal). Cross-validation against open-source watchdog datasets (Funky Citizens, Veridica) is recommended at the per-politician level when results are published.

---

#### `most_pro_russia@v1`

**Description.** Rank politicians by density of pro-Russia / NATO-skeptic / anti-Atlanticist alignment markers across delivered floor speeches in the time window. Default time window is post-2022 (`from: 2022-02-24`) — the most journalist-relevant horizon — but any window is accepted.

**Inputs.**
- `time_window` (default: `2022-02-24 → produced_at`; explicit override accepted).
- `chamber`, `min_speeches`, `min_words` (defaults).
- `marker_kinds` (optional): restrict to a subset (e.g. `["pro_russia_alignment", "nato_skeptic"]`).

**Filters.** All defaults. Adds:
- `custom_codings.geopolitical_alignment.markers[]` filtered to one of `pro_russia_alignment | nato_skeptic | anti_atlanticist | anti_american | china_aligned | ukraine_skeptic`.

**Score.**
- Substrate: filtered geopolitical-alignment markers in `voice == speaker_first_person`.
- Primary: **per-1000-words rate of pro-Russia-leaning markers**.
- Secondary: per-marker-kind rate; per-quarter trend (when window > 1 year).

**Output diffs.** Per-marker-kind breakdown column (separate `pro_russia_alignment` from `nato_skeptic` from `anti_american` — these load differently). Per-quarter trend column when window exceeds one year.

**Dependencies.** `geopolitical_alignment` custom rubric; voice classifier; persons registry.

**Build-order tier.** Runnable once the `geopolitical_alignment` rubric ships (build-order step 14).

**Notes.** Mark-and-evidence discipline is critical here — accusing a politician of "pro-Russia alignment" without an evidence-anchored marker is libel-shaped. The default voice filter handles the most common false-positive class (quoting and rejecting pro-Russia rhetoric); the deniable-wrapper parallel ranking is *also* mandatory because this is one of the rhetorical patterns most often deployed via apophasis (`nu spun că NATO ne-a forțat în Ucraina, dar...`) and weasel-attribution (`unii spun că sancțiunile ne fac mai mult rău decât bine`).

---

### Tier B — ranked deliberative-quality (positive axis)

#### `highest_dqi_speakers@v1`

**Description.** Rank politicians by deliberative quality (Steiner & Bächtiger DQI) across delivered floor speeches in the time window. DQI is the only positive-axis ranking — it answers *which speeches are good*, not *which are bad*.

**Inputs.** Same as `most_populist_politicians`.

**Filters.** All defaults. Adds:
- `framework == "dqi"`.

**Score.**
- DQI is multi-dimensional. The composite is an **explicitly weighted average of the six normalised sub-codings**, equal-weighted by default:
  - `level_of_justification / 3`
  - `content_of_justification`: `none → 0`, `group_interest → 0.33`, `mixed → 0.5`, `common_good → 1.0`
  - `respect_for_groups / 2`
  - `respect_for_demands / 2`
  - `respect_for_counterarguments / 2`
  - `constructive_politics`: `positional → 0`, `alternative_proposal → 0.66`, `mediating_proposal → 1.0`
- Composite ∈ [0, 1].
- Primary: **per-speech mean composite, ranked descending**.
- Secondary: 75th percentile; sum (intensity-weighted volume of high-DQI speeches).

**Output diffs.** Per-sub-coding breakdown column (the six dimensions). Politicians who rank highly on `level_of_justification` but poorly on `respect_for_counterarguments` are a different rhetorical type than politicians who rank highly across the board; the breakdown surfaces it.

**Dependencies.** `dqi` framework; voice classifier; persons registry.

**Build-order tier.** Runnable once DQI ships (build-order step 8 — second framework after Hawkins).

**Notes.** DQI has not been applied to Romania at corpus scale before (Q10 of the schema doc, risk #10). The framework rubric transfers in principle (justification, respect, constructive politics are language-neutral concepts), but Romanian-specific calibration is part of the eventual gold-sample work. Until then, `highest_dqi_speakers@v1` carries standard-rubric authority but not Romania-specific empirical anchoring; the methodology disclaimer notes this for DQI specifically.

The equal-weighting choice is documented and subject to revision (`@v2` would re-weight). Per Q4 of the schema doc, no composite is *stored*; the composite is computed at query time from the six stored sub-codings, and consumers can recompute under different weights from the same substrate.

---

### Tier C — cross-axis / divergence queries

#### `cross_axis_divergence@v1`

**Description.** The "left politician with right-wing speech" question class — and the cross-axis variants. Identifies politicians whose per-speech ideological placement (CMP RILE-econ for the economic axis, CHES GAL-TAN for the cultural axis) **diverges from the party-position-registry placement of their party at the speech-date**.

**Inputs.**
- `time_window`, `chamber` (defaults).
- `axis` (`econ | cultural | both`; default `both`).
- `divergence_definition` (`raw_delta | z_score | percentile | quadrant_flip`; default `quadrant_flip`).
- `min_speeches` (default 10, same as standard).

**Filters.** All defaults. Adds:
- For `axis == econ`: per-speech `cmp_left_right.score` and party-registry `manifesto-project.rile_econ` both available for the speaker's party at the speech date.
- For `axis == cultural`: per-speech `ches_gal_tan.score` and party-registry `ches.galtan` both available.

**Score.**
Per-speech divergence is computed under the chosen definition and aggregated per politician:

- **`raw_delta`**: `speech_score − party_score` for the speaker's party at speech date.
- **`z_score`**: `(speech_score − party_score) / σ_speeches_same_party_same_cycle`.
- **`percentile`**: speech's position in the distribution of same-party-same-cycle speeches.
- **`quadrant_flip`** (default): boolean — speech sits in the opposite quadrant from the party position. (Party RILE-econ < 0 *and* speech RILE-econ > 0 = "left party, right speech.") Most journalist-friendly framing.

Primary: **per-1000-words rate of divergent speeches** under the chosen definition. Secondary: mean divergence magnitude; per-axis breakdown when `axis == both`.

**Output diffs.** Two-column quadrant chart (econ-divergence × cultural-divergence) when `axis == both`. Each politician's row carries their position in the four-quadrant space. The quadrant chart is the single most journalistically valuable output of this query.

**Dependencies.** `cmp_left_right` and/or `ches_gal_tan` framework; voice classifier; persons registry; party-position registry with both CMP and CHES codings for the relevant party-cycles.

**Build-order tier.** Runnable once both per-speech CHES/CMP codings *and* the party-position registry are populated (build-order step 15).

**Notes.** Cross-axis is the more interesting query than single-axis — a politician whose speeches are economically-left but culturally-right is a real and well-documented phenomenon (Romanian PSD has a culturally-conservative wing; AUR is economically-statist but culturally far-right). Single-axis collapsing loses signal. Use `axis = both` as the default journalist-facing variant.

---

#### `party_manifesto_divergence@v1`

**Description.** For a single party in a single election cycle, compare the aggregate per-speech CMP-RILE / CHES-GAL-TAN of the party's parliamentary-floor speeches against the registry-stored manifesto-coded score for that party-cycle. Answers *"which parties' parliamentary rhetoric most diverges from their manifestos?"*

**Inputs.**
- `party_id` (required): registry id, e.g. `psd`.
- `election_cycle` (required): e.g. `2020`. Determines both the speech window (mandate produced by that election) and the registry-row joined.
- `axis` (`econ | cultural | both`; default `both`).

**Filters.** All defaults applied at speech eligibility; politician-eligibility threshold is **lowered to `>= 3 codable speeches per speaker`** because the aggregation is at party level and excluding minor speakers biases toward a few prolific MPs. The party itself must clear `>= 100 codable speeches` in window.

**Score.**
- Aggregate per-axis score: per-1000-words-weighted mean of per-speech CMP / CHES across the party's eligible speeches in the cycle.
- Divergence: aggregate − registry value.
- Reported with bootstrap CI on the aggregate.

**Output diffs.** Single-row output (one party-cycle, one axis-pair). When called with `axis == both`, two divergence numbers; the journalist-facing render is a 2D arrow from registry-position to aggregate-position in the (RILE-econ, GAL-TAN) plane.

**Dependencies.** Same as `cross_axis_divergence`.

**Build-order tier.** Same as `cross_axis_divergence`.

**Notes.** This query powers the *"Party X campaigned on Y but governed/spoke on Z"* feature class. Particularly journalistically valuable around election cycles. Pair with `party_rhetoric_shift@v1` for cross-cycle context.

---

### Tier D — registry-powered party / politician trajectories

#### `party_rhetoric_shift@v1`

**Description.** For a single party between two election cycles, compute the per-framework mean shift in parliamentary-floor rhetoric. Answers *"which party shifted its rhetoric most between these elections?"*

**Inputs.**
- `party_id` (required).
- `election_cycle_a`, `election_cycle_b` (required, in chronological order).
- `frameworks` (array, default `["hawkins_populism", "vparty_anti_pluralism", "cmp_left_right", "ches_gal_tan"]`).

**Filters.** Defaults; politician-eligibility lowered to `>= 3 codable speeches per speaker`.

**Score.**
For each framework: aggregate per-1000-words-weighted mean across the party's eligible speeches in each cycle; report shift (`b − a`) and bootstrap CI.

**Output diffs.** Per-framework row with shift magnitude and CI; a "did this shift exceed the registry's external-source shift" flag (cross-references CMP-RILE / CHES-GAL-TAN registry rows for the same party between the two cycles).

**Dependencies.** Per-framework codings + persons + party-position registry.

**Build-order tier.** Per-framework runnable as each framework ships; full output once Tier A and B are both up.

---

#### `politician_register_shift@v1`

**Description.** For a single politician, compare per-framework codings across **delivery contexts** — chamber speech vs committee statement vs joint-session vs interpellation vs question-register entry. Answers *"does this politician speak differently to MPs vs to committees vs in writing?"*

**Inputs.**
- `politician_id` (required, registry id).
- `time_window` (default: politician's full registry-active period).
- `frameworks` (array, default `["hawkins_populism", "dqi", "fallacies", "fearmongering"]` — the most-commonly-register-sensitive axes).

**Filters.** Default speech-eligibility *but* `delivery_mode` is allowed to span `delivered | written_interpellation | inserted_for_record`; politician-eligibility relaxed to `>= 3 speeches per delivery context`.

**Score.**
Per-framework, per-delivery-context aggregate (per-1000-words weighted). Report differences across contexts with CIs.

**Output diffs.** Per-context table; flag rows where context-vs-context delta exceeds 1.5× the politician's pooled standard deviation (the threshold for "register shift is real, not noise").

**Dependencies.** Per-framework codings + persons.

**Build-order tier.** Runnable as each framework ships.

**Notes.** The "Băsescu speaks differently to journalists than to MPs" pattern; useful for politicians who are caught register-switching across audiences. Romanian-specific: written interpellations follow much more formal templates than floor speeches, so a within-politician written-vs-delivered delta is partly a genre artefact and partly a register strategy; the disclaimer flags this explicitly.

---

### Tier E — audience-reception queries (zero-LLM)

#### `audience_disruption_index@v1`

**Description.** Rank politicians by audience-disruption density across delivered floor speeches in the time window. Disruption is defined from the canonical's narrator events: heckling, interruption, walkout-during-speech, mic-cut. Pure regex / enum aggregation over canonical narrator records — **no LLM coding required, no κ measurement required, no defamation surface beyond what the canonical already exposes** — and ships first per the schema doc's build order (step 6).

**Inputs.**
- `time_window`, `chamber`, `min_speeches`, `min_words` (defaults).

**Filters.** All defaults *except* coding-eligibility filters do not apply (no codings are involved).

**Score.**
- Substrate: `audience_signals` block on every per-speech analysis record.
- Primary: **per-1000-words rate of `(heckling + interruption + walkout_during_speech + mic_cut)` events**.
- Secondary: per-event-kind rates (heckling alone, walkout alone, mic-cut alone — these are *very* different events).

**Output diffs.** Per-event-kind table. The headline disruption-density number can mislead — a politician with one mic-cut and zero heckling vs a politician with 50 heckling events and zero mic-cuts have the same headline rate; the per-event breakdown surfaces the difference.

**Dependencies.** Canonical's `narrator.kind` enum (planned in `extraction-schema.md` v1.4); until then, regex over verbatim narrator text. Persons registry for politician aggregation.

**Build-order tier.** Build-order step 6 of the discourse-analysis schema. **Ships before any framework coding does** because it has no LLM-classifier dependency.

**Notes.** Per Q12 of the schema doc, this is the most defensible polarisation signal in the corpus — sustained applause from one party group plus heckling, walkout, or mic-cut from another is *cross-aisle disruption* evidenced by the chair's stenographic record. Cross-aisle cases (party-X applauds while party-Y heckles) are the most journalistically valuable variant but require party-source attribution on narrator events, which the canonical's current narrator parsing may not provide; see "Open questions for v1.1" below.

The methodology disclaimer for this query is *narrower* than the standard one — codings aren't involved, so neither human-κ nor model-snapshot caveats apply. The audience-reaction-signal-specific disclaimer (period-normalisation per stenographer practice — Q12 risk #11 of the schema doc) is rendered into `methodology_disclaimer` in place of the standard text.

---

### Tier F — search queries (filter, return matches with evidence)

Search queries are not rankings. They take a marker filter, return matching speeches with evidence spans, and are journalists' primary "show me concrete examples" tool. They share a common template; per-query specs only differ on the marker filter.

#### `speeches_with_marker@v1`

**Description.** Generic search. Return all speeches containing any marker matching the filter, with their evidence spans, voice annotations, and parent-document context.

**Inputs.**
- `marker_filter` (required): structured query over the codings tree, e.g. `{"custom_codings.romanian_specific.markers[].kind": ["antisemitism_explicit", "holocaust_relativisation"]}`.
- `time_window`, `chamber` (optional).
- `voice_filter` (default `["speaker_first_person"]`; pass `["all"]` to disable, in which case the deniable-wrapper / quoted / negated speeches are returned and the disclaimer flags the inclusion).
- `min_voice_confidence` (default 0.7; lowered to 0.5 with a disclaimer flag for exploratory passes).
- `era_window_check` (default `true`): when true, speeches whose marker fires outside the marker's defined `era_window` are flagged but not excluded; when false, no flag.
- `limit` (default 100).
- `sort` (`relevance | chronological | per_politician`; default `chronological`).

**Output.** Each row carries: `target_path`, `speaker_ref`, `chamber`, `session_date`, the matching `marker` records (with evidence spans + voice + voice_evidence + voice_confidence + attributed_to + era_window_status), the parent document's `agenda_item.title`, and a `flagged` status array (era-window violation, low-confidence, voice-uncertain, etc.).

**Disclaimer.** Standard methodology + model disclaimers, plus a per-row flag annotation when any of the row-level flags fire.

**Dependencies.** Whatever framework or custom rubric the `marker_filter` references.

**Build-order tier.** Runnable as the relevant rubric ships.

**Notes.** The most heavily-used query in journalist workflows. Common uses: *"all speeches by [politician] containing fearmongering markers in 2024"*; *"all speeches across the corpus containing `holocaust_relativisation` since 2010"*; *"all speeches containing `parallel_state` markers in [legislature]"*. The `voice_filter` default (first-person only) and the era-window flagging are the two safety rails that keep the output journalist-quotable.

---

## Versioning

Two version axes, parallel to the schema doc's discipline:

- **`conventions_version`** — bumped when the conventions section changes (a new universal filter, a new aggregation default, a new mandatory parallel ranking). All queries that don't pin a prior conventions version inherit. Major bump = field shape changes; minor = new mandatory field; patch = clarification.
- **`<query_name>@v<n>`** — per-query versioning. Major bump on filter rule change, scoring formula change, or output schema change. Minor bump on additive-only changes (new optional input, new output column). The prior version's spec stays in this file permanently so old publications remain reproducible — *queries are append-only*, never edited in place.

A query at `@v1` published in 2026 must be re-runnable in 2030 against current sidecars and produce results consistent with its 2026 publication, modulo new speeches entering the time window. If sidecars get re-coded under a new model or prompt, the underlying numbers will move; the query spec did not change.

## Adding a new query

Procedure:

1. **Open a PR** that appends a new query spec under the appropriate tier in the catalogue. Specify all sections: name, description, inputs, filters, score, output diffs, dependencies, build-order tier, notes.
2. **Pin to `@v1`**. The first release of any query starts at `@v1`. Don't publish anything as `@v0`.
3. **Reference an existing or new prompt and rubric**. Custom-rubric queries reference `prompts/<rubric>_v<n>.md`; framework-anchored queries reference the relevant framework version.
4. **Document the disclaimer**. Standard two-disclaimer template applies unless the query is a Tier E (no-LLM) or otherwise structurally different one; in those cases pin the override here.
5. **Smoke-test against the existing sidecar set**. Even when the underlying frameworks aren't fully populated, the query implementation should run end-to-end on whatever subset is available and produce a valid output object (possibly with empty rankings).

A query is "shipped" when its name appears in this file at `@v<n>`, the implementation produces an output object that schema-validates against the conventions output schema, and a sample run is committed to a future `validation/canonical_query_smoketests.jsonl`.

## Open questions for v1.1

These have not been pinned; collecting them so they don't get re-debated each time someone hits them.

1. **Cross-aisle disruption attribution.** `audience_disruption_index@v1` reports per-politician disruption density without separating cross-aisle (rival party heckles) from same-aisle (own party heckles). Cross-aisle is the more journalistically valuable signal, but the canonical's current narrator parsing may not attribute heckling to a party group. The v1.1 candidate `cross_aisle_disruption_index@v1.1` would require either (a) a small narrator-attribution pass (`(Aplauze din partea PSD.)`, `(Voci din partea AUR.)`) added to the canonical extractor, or (b) a heuristic at query time using surrounding-speaker party affiliations as a weak proxy.
2. **Floor-time normalisation.** The conventions doc's third normalisation basis (`markers / minutes_speaking`) is the cleanest accountability proxy but requires speech-duration metadata. The canonical does not yet carry duration; the obvious source is per-document timestamps in the stenogram (`Ședința a început la ora HH:MM` / closing time / per-segment `chair_segments[]`). Add a `duration_estimate_seconds` field to per-speech canonical records once the stenogram parser supports it; bump `conventions@v2` to make per-floor-time the default normalisation.
3. **Era-window strictness.** Currently the schema doc records `era_window` per marker definition and the validator emits a warning when the LLM tags a marker outside its window. The queries currently flag such rows (`era_window_check == true`) but still include them. Stricter mode — silently exclude — is cleaner from a defamation posture but loses the legitimate "explicit historical comparison" cases (e.g. a 2024 speech recalling 1990s `country_for_sale` rhetoric). v1.1 candidate: `era_window_strict_mode` input on every ranking query, off by default.
4. **DQI weighting alternatives.** `highest_dqi_speakers@v1` uses equal-weighted normalised composite of the six DQI sub-codings. The literature offers several alternative weightings (e.g. the original Steiner formulation weights `level_of_justification` and `respect` higher than `constructive_politics`). v1.1 candidate: `dqi_weighting` input with named alternatives (`equal | original_steiner | bachtiger_2017`).
5. **Mixed-confidence aggregations.** Currently every ranking has a "high-confidence-only" parallel ranking and an "all-codings" parallel ranking. A "weighted by confidence" aggregation (each speech's contribution to the score is multiplied by `framework_confidence × voice_confidence`) is a third option that combines the two without picking a hard threshold. Worth piloting before committing to the catalogue.
6. **Per-marker-kind defamation calibration.** Some custom markers carry markedly higher defamation risk than others (`antisemitism_explicit`, `holocaust_relativisation`, `legionar_adjacent`, `pro_russia_alignment`, `nato_skeptic`). v1.1 candidate: an extended disclaimer template for queries returning these markers, plus a higher default `min_voice_confidence` (0.85) and an automatic flag when the marker target is a named political opponent rather than an institution or abstract group.

These remain open questions until pinned in a future revision; the v1 prototype ships without them.

---

This document records the canonical query catalogue at conventions version `v1`. Subsequent revisions append new queries (preserving prior versions) and bump conventions when the universal defaults change. The schema substrate this catalogue depends on is recorded in [`discourse-analysis-schema.md`](./discourse-analysis-schema.md); the per-speech extraction substrate that everything stands on top of is recorded in [`extraction-schema.md`](./extraction-schema.md) and [`architecture.md`](./architecture.md).
