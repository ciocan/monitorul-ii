# Discourse-analysis schema — JSON sidecars on top of canonical extraction

Companion to [`architecture.md`](./architecture.md) and [`extraction-schema.md`](./extraction-schema.md). The architecture doc records the fetch + convert pipelines (PDF → markdown). The extraction-schema doc records the next layer (markdown → structured canonical JSON). **This file documents the layer above that: structured canonical JSON → discourse-analysis sidecars** that code each speech for populism, anti-pluralism, ideological positioning, rhetorical acts, and Romania-specific markers.

This doc is the design record. Every shape decision is paired with the rationale that produced it, the alternatives considered, and the failure mode each alternative would have produced. The consolidated schema appears at the end. Future contributors reading this should be able to reconstruct *why* the layer is shaped the way it is — not only *what* it is.

## Mission

The canonical schema serves three audiences (lawyers, journalists, historians/political scientists). The discourse-analysis layer adds a fourth question class for all three: **how** Romanian politicians spoke, not only **what** they did procedurally.

Concretely, this layer must enable queries like:

- *Which politician was the most populist in 2020?*
- *Which left-wing politician (by party affiliation) gave the most right-wing speeches?*
- *Which politicians use the most personal insults during plenary debate?*
- *Which politician scores highest on V-Party anti-pluralism?*
- *Which politicians use deniable-wrapper rhetoric (apophasis, weasel-attribution, dog-whistles) most heavily?*
- *How has Party X's rhetoric shifted between two electoral cycles?*
- *Which speeches attack the judiciary, opposition, media, or minorities?*
- *What rhetorical fallacies (false dichotomy, ad hominem, whataboutism) recur across debates on topic Y?*
- *Which politicians give the highest-quality deliberative speeches (DQI: justification level, respect, constructive politics)?*
- *Which speeches **securitize** an issue — frame it as existential to demand extraordinary or extra-constitutional measures?*
- *Which speeches deploy `statul paralel`, Soros, globalist-cabal, or other conspiracy-theory framings?*
- *Which politicians have aligned most pro-Russia / anti-NATO / anti-Atlanticist since 2014, and especially since 2022?*
- *Which speeches use Holocaust relativisation, Antonescu rehabilitation, or other Romanian historical-memory revisionist tropes?*
- *Which speeches contain antisemitic, misogynist, climate-denialist, or anti-vaccine markers?*
- *Which speeches generated the loudest cross-aisle disruption (sustained applause from one group, heckling/walkout/mic-cut from another)?*
- *Which politicians produced the most apologia (defensive rhetoric under accusation) when targeted by DNA proceedings?*

Every query above is composed at runtime from the same per-speech substrate. The schema does not pre-compute "demagogue index" or "authoritarian rating"; it records framework-defined codings on each speech-act, leaves composite indices to consumers, and demands that every coding be evidenced and auditable.

The mission constraints inherited from the canonical schema apply with even more force here:

- **Deterministic faithfulness over cleverness.** A speech denouncing populism must not be coded as populist. A quoted-and-rejected slur is not the speaker's slur.
- **Per-occurrence truth over derived "current" state.** A speech in 2018 is coded as it sat in 2018, regardless of how the politician's rhetoric evolved later.
- **Explicit unknowns over silent omission.** Low-confidence and uncertain-voice codings are recorded with their uncertainty, never coerced into false precision.

Two new constraints are specific to this layer:

- **Methodological defensibility over rich output.** Every coding must point to a published rubric or a versioned, validated custom rubric. "An LLM said so" is not an answer when a politician's lawyer asks how a populism score was computed.
- **Speech-act labelling, never personality attribution.** Records describe what was said, never who the speaker *is*. "This speech contains people-vs-elite framing under Hawkins's holistic-grading rubric" is a defensible academic claim. "Politician X is a populist" is a libel-shaped one. The schema enforces the distinction by storing only per-speech codings; politician-level rankings are derived at query time and explicitly framed as discourse-density measures, not personality attributions.

## Design tree

The schema was designed by walking the decision tree top-down, the same way the canonical schema was. Each subsection records a decision, its rejected alternatives, and the failure mode each rejection avoided. Thirteen decisions in total; the keystone safety decision is **Q5 (voice/quote-vs-claim attribution)** because it is the failure mode that scales catastrophically.

### 1. Storage location — sibling sidecar JSON, not inline canonical

**Decision.** A separate JSON file per canonical document: `<basename>.analysis.json` next to `<basename>.json`. Each analysis record is keyed back to the canonical by the existing hierarchical path (`mo://2026/PII/48#agenda/4/activity/7`). The canonical schema is not extended.

**Why a sibling sidecar.** The canonical schema is rigorously source-faithful: regex-extracted structure, with `topics.secondary` as the single bounded LLM-judgment field. Discourse codings (populism, authoritarianism, fearmongering, rhetorical fallacies) are several orders of magnitude more interpretive — a different epistemic class. Mixing them inline blurs the line between "what the source says" and "how we coded it". Lawyers, journalists, and researchers each need that line sharp for different reasons:

- Lawyers reading legislative intent want a clean source-only record.
- Journalists want to surface "verbatim" vs "coded" distinctly to readers.
- Researchers want to swap rubrics without contaminating the source record.

A sibling layer keeps all three distinctions clean.

**Why not inline.** Three failure modes:

1. **Schema-version churn.** Classifiers and rubrics will iterate orders of magnitude faster than the source structure. Every classifier upgrade or rubric refinement would bump `schema_version` and force a corpus-wide migration of canonical records — for changes that have nothing to do with the source.
2. **Spurious `content_sha` invalidation.** Every record carries a content fingerprint for drift detection. Re-coding under a new framework would change the canonical record's hash even though the source markdown is identical, breaking downstream embedding caches and re-extraction signals.
3. **Epistemic muddle.** A canonical record is supposed to be the deterministic representation of the source. Inlining LLM-judgment fields makes the canonical record itself non-deterministic across re-runs.

**Why not compute-on-demand only.** Per-speech codings must be deterministic, citable, and stable across queries — a paper that ranks the most-populist politician of 2020 must be replicable next year on the same data. Compute-on-demand gives different answers across query timestamps, costs explode at query time, and there is no audit trail when a finding is contested.

**Pipeline alignment.** The existing pipeline is a chain of independent idempotent stages: `.pdf → .md → .json (canonical)`. The analysis layer adds `→ .analysis.json (discourse)` as the next stage. Each stage owns one concern, can be re-run alone, can be skipped or deleted without corrupting upstream. A future `monitorul-ii analyze` subcommand mirrors `monitorul-ii convert` exactly in shape.

**Multi-rubric overlay.** Different consumers want different frameworks (Hawkins populism, V-Party anti-pluralism, Manifesto Project economic axis, CHES cultural axis, V-Dem liberal-democracy attacks). A sidecar can hold all of them as independent subtrees with their own version stamps. Upgrading one framework rewrites only that subtree; inline would force one rubric per field or namespaced bloat.

**Cost containment.** Re-extracting canonical structure across the whole corpus is cheap (regex on ~1,200 documents). Re-coding rhetoric across the whole corpus is expensive (LLM calls scaling with speech-count). Sidecar means the rhetoric pipeline can run lazily, on demand, or selectively (only post-2014 corpus, only Camera, only the sample needed for a paper).

**Defensibility / deletability.** If a politician contests a populism score on legal grounds, the analysis layer can be rebuilt under a revised rubric, regenerated for the disputed period, without touching the canonical. If a court orders a finding redacted, the offending sidecar record is deleted by hierarchical path; the canonical "what was said" record stays intact.

### 2. Unit of attribution — speech-acts, not politician-traits

**Decision.** One coding record per speech-act (a speech, an interpellation, a vote justification — anything a politician says, identified by the canonical's hierarchical path). The schema never records "Politician X is a populist". It records "this speech contains markers W, X, Y under framework F." Politician-level rankings are derived at query time by aggregating per-speech codings.

**Why per-speech.** Five reasons, in priority order:

1. **Defamation safety.** "This speech contains people-vs-elite framing under Hawkins's holistic-grading rubric, evidenced at lines 412–418" is a methodologically defensible academic claim — it points to text, names a published rubric, and labels a speech-act. "Politician X scores 0.83 on populism" is a personality attribution that sits in libel territory under Romanian Civil Code articles 252–256 (right to dignity, image, private life). Same data, very different legal posture. The sidecar should never make personality claims; it should make speech-act claims that aggregate into politician profiles only when a consumer constructs a documented query.
2. **Per-occurrence truth, matching the canonical.** The canonical schema commits to per-occurrence truth: party affiliation is recorded *as attributed at this moment in this document*, never as "current party". The same principle applies to discourse: a populist *speech* in 2020 is a fact about that speech forever, regardless of whether the speaker later moderates or radicalises. Storing per-politician rollups would force "what was Politician X's populism score in 2020" to be an immutable historical fact written down — but that's a derived statistic whose inputs (which speeches counted, which didn't) and methodology (which framework, what threshold) will keep evolving. Rollups go stale; per-speech codings don't.
3. **Reproducibility and aggregation flexibility.** A researcher running a paper needs to be able to say "I ranked politicians by mean Hawkins-graded populism across all plenary speeches in 2020 with at least 100 words, weighted by floor-time." That is a query, parameterised by inclusion threshold, aggregation function, normalisation basis, and confidence filter. The schema must enable many such queries from the same data. Per-speech is the substrate that all queries can express; per-politician rollups commit to one such query as canonical.
4. **Composability with party-switching.** The "left politician with right-wing speech" question crosses party-switches. A politician might be PSD in 2018, independent in 2020, then a member of a splinter party in 2024. Per-speech coding cleanly handles "speeches where rhetoric placement diverges from the party-at-the-time placement". Per-politician rollups would have to choose: rollup over current party? past party? all-time? Every choice is wrong for some legitimate query.
5. **Re-coding economics.** When the LLM or rubric improves, you re-code per speech, then re-aggregate — the aggregations are free recomputes from cached per-speech records. If rollups were canonical, every classifier upgrade would force every politician profile to be recomputed and rewritten — same cost, but with no clear "where did this politician's number come from" audit trail.

**Why not per-politician-per-period rollups.** See (1)–(5). The keystone is (1): politician-trait records are libel-shaped from day one.

**Why not both layers persisted.** Maintaining two layers in sync is exactly the kind of denormalisation debt the canonical schema rejected (it explicitly chose hierarchical-canonical over materialised flat projections, deferring projections to ES ingest). The same logic applies here: per-speech is canonical; per-politician rollups are an ES-ingest concern, not a sidecar concern.

### 3. Framework anchoring — hybrid: published frameworks for primary axes, marked custom rubrics elsewhere, no "lies" axis

**Decision.** Use peer-reviewed published frameworks for every primary axis where one exists; use *explicitly-marked* custom rubrics only for axes that no peer-reviewed per-speech framework covers; **drop "lies" entirely** and replace it with rhetorical-fallacy markers that are detectable from text alone.

**The state of the art.** Each label the user might want has a different academic infrastructure behind it. Pretending they are equivalent is a category error.

| Axis | Published framework | Maturity |
|---|---|---|
| Populism | **Hawkins's holistic grading** (0/1/2 textbook scale) — used by Global Populism Database, validated at κ ≈ 0.7 inter-coder agreement | Excellent |
| Deliberative quality (positive axis) | **Discourse Quality Index (DQI)** — Steiner & Bächtiger; level of justification, group-vs-common-good appeal, respect, constructive politics | Excellent — 30+ comparative parliamentary studies, κ ≈ 0.7–0.8 |
| Left–right (economic) | **Manifesto Project (CMP/RILE-econ)** — Romanian-party data since 1990 | Excellent |
| Cultural axis (GAL–TAN) | **Chapel Hill Expert Survey (CHES)** — Romanian-party expert codings | Excellent |
| Anti-pluralism / illiberal | **V-Party** anti-pluralism index, **V-Dem** liberal-democracy sub-indicators (attacks on judiciary / opposition / media / minorities) | Good |
| Securitization (existential framing → demand for extraordinary measures) | **Copenhagen School** (Buzan, Wæver, de Wilde) — speech-act of moving an issue out of normal politics into emergency politics | Conceptual framework, no per-speech numeric scale; tractable as marker pattern |
| Authoritarian discourse | Norris–Inglehart libertarian-authoritarian; Stenner cultural-authoritarianism | Mature for individual surveys; weaker for per-speech coding |
| Demagoguery | Patricia Roberts-Miller's framework; otherwise treated as a *composite* of populism + ad-hominem + scapegoating + fear-appeal | No single accepted scale |
| Insults / ad-hominem | NLP literature (Perspective API, HateBase) + custom dictionaries | No political-science gold standard, but technical infrastructure exists |
| Fearmongering / threat narrative | van Dijk's discourse-analytic threat construction | Conceptual, not a per-speech scale |
| Victimhood narrative | Vollhardt collective-victimhood; Bar-Tal | Conceptual, not a per-speech scale |
| Apologia (defense under accusation) | **Ware & Linkugel** apologia genres — denial / bolstering / differentiation / transcendence | Conceptual, mature in rhetorical studies; tractable as marker patterns |
| Conspiracy framing | Uscinski–Parent conspiracy-mentality; Bratich political conspiracy panic | Mature in political psychology; tractable as marker patterns |
| **"Lies" / misinformation** | **None applicable** — this is fact-checking, not text analysis | Don't pretend |

**Why hybrid.**

1. **Credibility ceiling.** Published frameworks bring decades of validation, replication studies, and inter-coder agreement statistics. Anchoring lets the project say "we coded these speeches under Hawkins's holistic-grading framework (used by 60+ peer-reviewed studies)." Anchoring to an internal rubric caps the corpus at "interesting prototype" — academics will not cite it, journalists who quote it will get challenged on methodology, and a politician's lawyer will dismantle it.
2. **Cross-national comparison is the killer feature.** The Global Populism Database has Hawkins-coded speeches for ~1,900 leaders globally. V-Party already has Romanian parties scored on six axes. Manifesto Project has every Romanian party manifesto coded since 1990. Anchoring to these frameworks gives free comparison with Orbán, Kaczyński, Le Pen, Erdoğan, Trump, Modi, etc., out of the box. Romanian-only custom rubrics give nothing comparable.
3. **Legal defensibility.** When a politician contests a coding, "your speech scored 1 on the Hawkins 0–2 scale, evidence at lines 412–418, framework cited in 60+ peer-reviewed studies" is a much stronger position than "our team developed a custom Romanian populism rubric." Same evidence, very different posture.
4. **Romania-specific gaps are real but bounded.** Some genuinely Romanian patterns will not appear in any international framework: anti-Hungarian rhetoric in UDMR-debate context; Bessarabia/Moldova ethno-territorial framing; PSD vs anti-PSD post-2017 protest-era tropes; anti-justice-reform narratives from the 2018–2019 OUG 13 era; legionar / Iron-Guard adjacency markers; ortodoxist-nationalist religious framing; anti-Roma rhetoric. These need custom rubrics — but they should sit *alongside* the published-framework codings, marked clearly as `framework: "custom-romanian@0.1"`, never replacing the international anchors.
5. **"Lies" is a trap.** Coding "this politician lies" requires ground-truth fact-checking — not text-only analysis. There is no defensible automatable solution. Two acceptable substitutes:
   - **Detect rhetorical patterns associated with misleading argument** (false dichotomy, strawman, ad hominem, equivocation, hasty generalisation, whataboutism). These are text-detectable, well-defined in argumentation theory, and a defensible proxy for "rhetorical dishonesty" without making truth-claims.
   - **Cross-reference fact-checking organisations** (Funky Citizens, Veridica, Factual.ro have done individual claim fact-checks). Link those rather than assert the "lie" yourself.
   The schema implements the first; the second is deferred to a cross-document linker pass.
6. **Insults / fearmongering / victimhood need explicitly-marked custom rubrics.** No published framework codes these per speech. They are methodologically tractable as marker-based detection (insult dictionary + LLM judgment for context; threat-frame markers; in-group/out-group blame markers). The discipline required:
   - Document the rubric in `prompts/<framework>_v1.md`.
   - Version-pin the rubric (e.g. `custom-fearmongering@0.1`, `custom-insults@0.1` — one rubric version per phenomenon, per Q4).
   - Validate against a hand-coded sample.
   - Accept the lower defensibility.
   - **Never combine custom-rubric scores with framework-rubric scores into a single composite number.** Custom and framework codings live in separate fields; consumers know which is which.
7. **"Demagogue" and "illiberal" are composite labels, not primary axes.** Demagoguery ≈ populism + ad-hominem + scapegoating + fear-appeal. Illiberal ≈ low V-Dem-liberal-democracy + high V-Party-anti-pluralism + attacks-on-judiciary markers. Neither has a per-speech coding rubric of its own. Code the components; let consumers aggregate. A `demagoguery_index` is a derived statistic, not a stored field — same logic the canonical schema applied to "current party" vs per-occurrence party_group.
8. **DQI is the positive axis the layer would otherwise lack.** Every other primary framework here codes a pathology — populism, anti-pluralism, illiberal attacks. Without a positive deliberative-quality measure the schema can only answer questions about *which speeches are bad*, never *which speeches are good*. Comparative claims like "Politician X balances populist tropes against high-quality argumentation" become unsayable. **Steiner & Bächtiger's DQI fixes the asymmetry without inventing a custom rubric**: it's the parliamentary-deliberation gold standard with thirty-plus comparative studies behind it (Switzerland's Bundestag, US Congress, EU Parliament), published κ ≈ 0.7–0.8, and a closed marker set that maps cleanly onto Romanian floor speech. Crucially, it answers a question the populism literature cannot: *"is this speech arguing or just attacking?"*
9. **Securitization is distinct from generic fearmongering and predicts different outcomes.** "X is a scary threat" and "X is an existential threat that justifies suspending normal procedure" look similar at the lexical level but predict very different politics. The fearmongering markers capture threat construction; securitization captures the *speech-act* of demanding extra-constitutional response. A speech can fearmonger about Hungarians without securitizing; a speech can securitize migration without naming any ethnic group. Conflating them blurs the most consequential illiberal-rhetoric signal in the corpus. Keep them as separate marker families with separate prompts.

**Why not roll-your-own.** See (1)–(3). Caps the corpus at "weekend project that journalists write a one-off article about" and forfeits all cross-national comparability.

**Why not pure published frameworks.** See (4)–(6). Some real-world rhetorical phenomena have no published per-speech coding framework, including Romania-specific signals. Refusing to capture them at all means leaving real signal on the floor.

### 4. Score representation — native-unit per framework, markers, evidence spans, confidence; no stored composites

**Decision.** Each per-speech, per-framework coding carries:

- **Native-unit score** in the framework's own terms (Hawkins → 0/1/2 categorical; V-Party → 0–1 continuous; CMP → scaled left-right; CHES → 0–10 GAL-TAN).
- **`score_unit`** documenting the unit family (`ordinal_0_2`, `continuous_0_1`, `rile_econ`, `galtan_0_10`).
- **`framework_confidence`** — the model's self-reported confidence in the coding, separate from the score.
- **Markers** — framework-specific linguistic features detected, drawn from a closed framework-defined vocabulary, each with its own `evidence` `SourceSpan`.
- **`rationale`** (optional) — short LLM-generated explanation of why the coding came out as it did.

No composite scores are stored. No "demagogue index", no "authoritarianism rating", no "rhetoric quality". Composites are query-layer concerns.

**Why match the framework's native unit.** Hawkins's holistic grading is *categorical* (0 = no populism, 1 = mild, 2 = fully populist). V-Party indices are *continuous* 0–1. CMP is a scaled left-right index from per-quasi-sentence category counts. CHES is a 0–10 expert scale. Forcing them into a single numeric field would erase the framework's own measurement structure and force fake precision (Hawkins cannot honestly produce `0.83`). Each coding records its score in its native unit and pins the framework version that defines it.

**Why markers + evidence spans are load-bearing.** "This speech scored 2 on Hawkins" is not enough for legal defence. "This speech scored 2 on Hawkins because of [people-vs-elite framing at lines 412–418, moralistic-Manichaeism at lines 423–426, homogeneous-people invocation at lines 445–450]" is. Markers explain the score in framework terms; evidence lets a reader verify by reading the source. This mirrors the canonical schema's `SourceSpan` per record and per reference; the discourse layer extends the same audit-trail discipline to per-marker granularity.

**Why confidence is a separate field, not folded into the score.** A confident `Hawkins=0` and a low-confidence `Hawkins=2` are very different epistemic objects. Aggregations should be able to filter (`framework_confidence > 0.7` only) before ranking. Mixing confidence into the score loses this. Two fields: `score` (what the framework says) plus `framework_confidence` (how sure the model is the framework was applied correctly).

**Why no stored composites.** This is the keystone trap of discourse-analysis schemas: the temptation to write `demagoguery_score = 0.6 × populism + 0.3 × ad_hominem + 0.1 × fearmongering`. The weighting is a *consumer concern*. Different journalists and researchers will pick different weights; they all want access to the same components. Storing a composite commits the canonical to one weighting forever, hides the inputs, and is unauditable when challenged. Composites are queries, not fields. Same architectural logic the canonical schema applied when it refused to materialise flat projections (those belong in ES ingest).

**Why markers come from a closed framework-specific vocabulary.** Free-form marker labels would degenerate into 5,000 distinct strings, the same problem `topics.secondary` solves with the closed-primary / open-secondary split in the canonical schema. Each framework defines its own closed marker set, documented in `registries/framework_marker_definitions.json`:

- **Hawkins's 7-marker rubric:** `people_vs_elite`, `moralistic_manichaeism`, `homogeneous_people`, `evil_elite`, `popular_will_supremacy`, `crisis_invocation`, `cosmic_proportions`.
- **DQI (Steiner–Bächtiger) closed marker set:** `level_of_justification` (ordinal 0–3: none / inferior / qualified / sophisticated), `content_of_justification` (`group_interest | common_good | mixed | none`), `respect_for_groups` (0–2), `respect_for_demands` (0–2), `respect_for_counterarguments` (0–2: ignored / acknowledged / engaged-with), `constructive_politics` (`positional | alternative_proposal | mediating_proposal`). DQI is multi-dimensional: each speech carries the six sub-codings rather than a single composite, matching the published rubric exactly.
- **V-Party anti-pluralism:** `opposition_delegitimization`, `media_hostility`, `judiciary_attack`, `minority_scapegoating`, `democratic_norms_rejection`.
- **V-Dem attacks-on:** `judiciary`, `opposition`, `media`, `minorities`, `civil_society`.
- **CMP RILE-econ marker_counts:** the CMP per-category vocabulary (per501 environmental protection, per503 social justice, per401 free enterprise, per414 economic orthodoxy, etc.).
- **Securitization markers (custom, Copenhagen-anchored):** `existential_framing`, `referent_object_named` (the thing-being-protected: nation, civilisation, family, sovereignty, ethnic-group), `extraordinary_measures_invoked` (suspension of rules, emergency powers, derogations), `audience_acceptance_appeal`, `enemy_designation`. The diagnostic *securitization move* is the co-occurrence pattern (`existential_framing → referent_object → extraordinary_measures_invoked`), recorded as a derived `securitization_move_present: bool` per speech with its constituent markers evidenced.
- **Custom fallacies:** `false_dichotomy`, `strawman`, `ad_hominem`, `equivocation`, `hasty_generalization`, `whataboutism`, `appeal_to_emotion`, `slippery_slope`, `loaded_question`, `red_herring`, `circular_reasoning`.
- **Custom insults:** structured records with `term`, `target` (`{ name, kind }` where kind ∈ `individual | group | institution`), and evidence; the marker is the act, not a free string.
- **Custom fearmongering markers:** `existential_threat`, `civilizational_decline`, `invasion`, `demographic_replacement`, `economic_collapse`, `cultural_extinction`, `enemy_within`.
- **Custom victimhood markers:** `we_are_victims`, `scapegoat_blame`, `persecution_narrative`, `historical_grievance`, `innocent_in_group`, `monstrous_out_group`.
- **Custom apologia markers (Ware–Linkugel):** `denial`, `bolstering` (invoking past good acts to offset accusation), `differentiation` (separating self from a tainted category), `transcendence` (reframing to a higher principle), `counter_attack`, `victim_self_framing`. Apologia is its own bucket because defensive rhetoric under accusation has a distinct audience and structure from offense.
- **Custom conspiracy-framing markers:** `parallel_state` (the post-2017 Romanian `statul paralel` trope), `soros_conspiracy`, `globalist_cabal`, `deep_state`, `cultural_marxism`, `they_dont_want_you_to_know` (epistemic-conspiracy framing), `false_flag_accusation`, `hidden_hand`, `replacement_theory`, `medical_conspiracy`. Distinct from fearmongering: a conspiracy frame *names a hidden-orchestrator agency*; pure fearmongering need not.
- **Custom geopolitical-alignment markers:** `pro_russia_alignment`, `nato_skeptic`, `anti_atlanticist`, `anti_american`, `eurosceptic_soft`, `eurosceptic_hard`, `pro_eu_federalist`, `china_aligned`, `bessarabia_irredentist` (also Romania-specific), `ukraine_solidarity`, `ukraine_skeptic`. Geopolitical alignment is its own axis because `anti_eu_sovereignty` (a single marker) collapses too much.
- **Custom gendered-rhetoric markers:** `gendered_insult`, `appearance_attack`, `paternalism`, `traditional_family_weaponized`, `reproductive_rights_attack`, `sexist_dismissal` (e.g. `doamna deputat` used dismissively), `motherhood_invoked_as_disqualifier`. Surprisingly absent in v0.1 given Diana Șoșoacă's incidents, the 2018 family-referendum debates, and recurrent floor-level femicide-policy framing.
- **Custom climate-denial markers:** `climate_denial`, `climate_alarmism_dismissed`, `green_transition_attack`, `eco_obstruction`, `fossil_fuel_defense`. Emerging issue; will only grow.
- **Custom pandemic / public-health markers:** `anti_vaccine`, `anti_lockdown`, `medical_conspiracy`, `tradition_over_science`, `bodily_autonomy_invoked`. COVID-19 produced a vocabulary that mobilised AUR/SOS-România and won't be visible without a dedicated bucket.
- **Custom Romanian-specific markers (expanded for v0.2):** `anti_hungarian`, `anti_roma`, `anti_lgbt`, `anti_justice_reform`, `dna_defense` (positive-side counterpart), `bessarabia_irredentist`, `legionar_adjacent`, `antonescu_rehabilitation`, `holocaust_relativisation`, `antisemitism_explicit` (distinct from `legionar_adjacent`), `securist_accusation`, `ortodoxist_nationalist`, `anti_eu_sovereignty`, `traseism_accusation`, `parallel_state` (also under conspiracy), `country_for_sale` (`au vândut țara`, `colonie`, `țara la mâna a doua`), `orange_mafia` (`mafia portocalie`, anti-Băsescu-era), `băsism` (anti-Băsescu-period framing as cultural marker), `iliescism`, `comunisto_securist`, `corectitudine_politica` (anti-`woke` framing), `marxism_cultural` (imported anglo-right vocabulary), `romanian_diaspora_invoked`, `second_class_country_complex` (`țară de mâna a doua`).

Closed sets, version-pinned, documented in a single dictionary file. Adding a marker is a registry change with a version bump.

**Era-scoped markers.** Some markers presuppose a temporal context: `parallel_state` is post-2017, `băsism` is post-2004, `comunisto_securist` post-1989, `country_for_sale` peaks during privatisation debates. Each marker definition optionally carries an `era_window` (`{from: "YYYY-MM-DD", to: "YYYY-MM-DD" | null}`); the validator warns when the LLM tags a marker outside its window so anachronisms surface during validation rather than corrupting rankings.

**Why split the original `rhetorical_acts` bucket into per-phenomenon buckets.** The v0.1 design lumped insults, fearmongering, victimhood, and fallacies under one `custom-rhet-acts@0.1` rubric version. They are phenomenologically distinct, have different prompts, different validators, different κ baselines, and evolve at different rates. Folding them under one rubric forced a single version bump whenever any one component changed. **v0.2 splits them into independent custom subtrees** (`insults`, `fearmongering`, `victimhood`, `fallacies`), each with its own `rubric_version` and `prompt_version`. Same reasoning the canonical schema applied when refusing to materialise composite scores: each phenomenon is its own measurement object.

**Why custom-rubric markers do not need a numeric "score" field.** For framework-anchored axes (Hawkins, V-Party, CMP, CHES, V-Dem), the score is the framework's primary output. For custom-rubric markers (insults, fearmongering, victimhood, fallacies), the schema records *the marker itself* with target/evidence; the "score" is implicit (count of markers, possibly normalised by speech length at query time). An insult is a thing detected with a target and evidence; it does not need its own numeric score, just a structured record.

### 5. Quote-vs-claim attribution — two-pass extraction, voice persisted per marker, separate rhetorical-moves bucket for deniable wrappers

**Decision.** Every marker carries a `voice` field. Voice is determined by a two-pass extractor: first pass detects quote/report/negation/hypothetical regions in the speech text (regex hints + LLM for ambiguous cases); second pass codes markers and records voice. Apophasis and weasel-attribution patterns are *additionally* captured as `rhetorical_moves` — separate marker kinds that record the deniable-wrapper move itself, even when the literal voice is `negated` or `attributed`.

**Why this is the keystone safety decision.** Without voice tracking, the corpus's ranked lists will mix populism-deniers with populism-users — and the deniers, who quote populist tropes more often than they utter their own (in order to denounce them), will rank higher for populism than the actual populists. The whole product breaks. Worse, tagging a politician's speech denouncing anti-Hungarian rhetoric as anti-Hungarian rhetoric is the worst possible failure mode: it inverts the speaker's actual stance and is straightforwardly defamatory. **This is the failure mode that scales catastrophically; everything else in this schema is solvable, but voice is not.**

**Voice constructions in Romanian parliamentary text.**

- **Direct quote** — `citez:`, `așa cum a afirmat...`, `domnul X a spus că...`, `parafrazez...`.
- **Reported speech** — `X susține că Y`, `X pretinde că Y`, `X consideră că Y`, `X a declarat că Y`.
- **Negation** — `nu este adevărat că...`, `nu putem accepta că...`, `nu este corect ce spune...`, `respinge afirmația că...`.
- **Hypothetical / conditional** — `dacă cineva ar spune că...`, `s-ar putea crede că...`, `presupunând că...`.
- **Apophasis / paralepsis (deniable plant)** — `nu spun că X, dar...`, `unii spun că X...`, `mulți cred că X...`, `nu vreau să afirm X, însă...`.
- **Sarcasm / irony** — `desigur că X` said sarcastically; only resolvable in context.

**Voice enum (closed).** `speaker_first_person | quoted | reported | negated | hypothetical | apophasis_disclaimed | weasel_attribution | sarcastic | uncertain`. The default value is `speaker_first_person`. The explicit `uncertain` is mandatory when the classifier is not confident — *no `null` allowed*. Forcing the classifier to declare uncertainty rather than guessing keeps consumer aggregations honest.

**Why Hawkins's framework requires voice handling.** Hawkins's holistic-grading instructions explicitly tell coders to assess the speaker's worldview *as expressed in first-person assertion*. Quoting an opponent's framing, even at length, is not coded as the quoter's worldview. If the schema ignores voice, the codings are not Hawkins-compliant — and the corpus cannot honestly be published as Hawkins-graded. The same standard applies to V-Party (assesses *the party's* anti-pluralism, not what the party quotes) and to CHES/CMP (assess the party-manifesto-level position, not quoted-and-rejected positions).

**Why apophasis deserves its own marker bucket.** "Eu nu spun că maghiarii vor să cucerească Transilvania, dar..." is a *rhetorical move*, not a sincere disclaimer. The literal voice is `apophasis_disclaimed`, but the rhetorical effect is *the claim has been planted in the audience's mind*. Two coding layers on the same evidence span:

- **Literal voice** of the marker (matches what a syntactic-voice classifier sees: `apophasis_disclaimed`, `weasel_attribution`, `quoted`, etc.).
- **Rhetorical-act marker** for the deniable-wrapper move itself: `apophasis`, `weasel_attribution`, `dog_whistle`, `innuendo`, `rhetorical_question_loaded`, `sarcastic_endorsement`, `prefacing_then_claiming` (`nu spun X, dar...` then asserts X), `just_asking_questions` (epistemic deniability through interrogative form), `concern_trolling` (faux-sympathetic warning that delegitimises target), `false_concession` (yields a minor point to launch a major one), `ironic_distancing` (uses target's language to mock it). The catalogue is intentionally generous because each move predicts a different defamation posture and a different consumer query.

A consumer who wants "what did the speaker assert in their own voice?" filters by literal voice (`speaker_first_person` only). A consumer who wants "where are the deniable-wrapper rhetorical moves?" queries the `rhetorical_moves` bucket. **The keystone product principle: published rankings always report both first-person and deniable-wrapper figures side by side — collapsing them flatters the more cunning operators.**

**Why two-pass is more reliable than single-pass.** Asking an LLM to simultaneously detect the marker, decide the voice, and detect the deniable-wrapper move in one prompt produces fragile, high-variance output. Pre-processing voice regions (regex catches ~70 % deterministically — quote-introducer cues, negation cues, apophasis templates; LLM resolves the rest) gives the marker classifier a structured input — improving inter-run consistency and lowering cost. The two-pass split is an extractor implementation discipline, not a schema field; the schema records the result (per-marker voice) regardless of how it was derived.

**Voice provenance.** The voice judgment itself carries a `voice_confidence` and a `voice_evidence` `SourceSpan` (the cue phrase: `citez:`, `nu spun că ... dar`, `X susține că`). When `voice ∈ {quoted, reported}`, the schema also records `attributed_to: Speaker | null` to identify who is being quoted.

**Why not ignore voice.** Defamation risk explodes; ranking quality inverts; published frameworks become unfaithful; the product collapses.

**Why not voice without a two-pass strategy.** Single-pass extractors underperform on this task; the next year would be spent debugging voice-misattribution bugs instead of shipping new analyses.

### 6. Party-position decoupling — separate party-position registry, divergence is a query

**Decision.** Party-level ideological positions (CMP, CHES, V-Party scores per party per cycle) live in a **separate registry**: `registries/party_positions.json`. Per-speech ideological coding (Q3's `cmp_left_right`, `ches_gal_tan`) records the *speech-content* placement. The "left politician with right-wing speech" question is a documented query that joins per-speech coding with party-position registry; **divergence is computed at query time, never stored**.

**Why a separate registry.**

1. **Pattern reuse.** The canonical schema's roadmap already includes three external registries: `persons` (`Speaker.person_id`), `legislation` (`Reference.*_id`), `ministries` (`addressed_to_normalized`). Party positions are the fourth registry of the same shape — externally-curated reference data, joined at query time.
2. **Drift containment.** Manifesto Project releases roughly annually after each election cycle; CHES releases every 4 years; V-Party releases per election. When a new round publishes (CHES 2028, V-Party post-2028 elections), the registry updates with one file edit; per-speech analysis records remain untouched. Denormalising party scores into per-speech analysis records would invalidate every analysis record on every external-source update.
3. **Time-of-speech accuracy.** Party scores must be matched to the *speech date* — a speech from 2014 should join PSD's CMP-2012 / CHES-2014 score, not PSD's CMP-2024 score. The registry stores the time-series; the join picks the temporally-nearest coding. Per-speech denormalisation tempts a single "current" score per party that is wrong for old speeches.
4. **Multi-framework comparability.** Different researchers want CMP-RILE for economic-axis questions, CHES-GAL-TAN for cultural-axis, V-Party for anti-pluralism. The registry stores all three (and more) per party-cycle; the consumer picks. Per-speech denormalisation would either commit to one or bloat every record.
5. **Party-name normalisation belongs at the join.** "PSD", "Partidul Social Democrat", "P.S.D.", "Pro România" (a PSD-splinter), "PSDM" — the canonical schema records `party_group` verbatim from the chair's announcement, which is the right discipline for source-faithfulness. Mapping to a normalised expert-coded entity ("PSD as expert-coded by CMP and CHES") is a registry concern: the registry maintains an alias table, the join consults it, no per-speech record is polluted.
6. **Divergence is a query.** Same architectural logic as Q4 (no stored composites). Multiple definitions of "divergence" exist:
   - Raw delta (`speech_score − party_score`).
   - Z-score (`(speech_score − party_score) / σ_party_speeches`).
   - Percentile within speeches by same-party speakers in same cycle.
   - Quadrant flip (party RILE-econ < 0 *and* speech RILE-econ > 0 = "left party, right speech" cross-axis case).
   Storing one definition commits the canonical to it forever. Define the query patterns in `docs/canonical-queries.md`; let consumers pick.

**Why two axes (econ + cultural) matter.** The most interesting divergence cases are *cross-axis*. A politician whose speeches are economically-left but culturally-right (anti-LGBT, anti-immigration, anti-Roma) is a real and well-documented phenomenon (Romanian PSD has a culturally-conservative wing; AUR is economically-statist but culturally-far-right). Two-dimensional divergence — econ-divergence + cultural-divergence — is the meaningful query; collapsing to a single left-right axis loses signal.

**Why time-window matching uses election cycles.** A speech from 2018-03-15 joins PSD's CMP-2016 (the 2016-election manifesto-coded position) rather than the 2020 one; the active manifesto and party programme during a parliamentary mandate is the one from the election that produced that mandate. This is the documented default; consumers can override.

**Concrete registry record:**

```json
{
  "id": "psd",
  "canonical_name": "Partidul Social Democrat",
  "aliases": ["PSD", "P.S.D.", "Partidul Social Democrat",
              "Grupul parlamentar PSD", "Grupul parlamentar al PSD"],
  "active": { "from": "1993-07-10", "to": null },
  "successor_relations": [{ "kind": "splinter_origin", "from": "fsdn_1992", "year": 1993 }],
  "codings": [
    { "framework": "manifesto-project", "framework_version": "MARPOR_2024-1",
      "election_year": 2020, "rile": -8.2, "rile_econ": -12.4, "gal_tan": null,
      "source_url": "https://manifesto-project.wzb.eu/..." },
    { "framework": "ches", "framework_version": "CHES_2024", "wave_year": 2024,
      "lrgen": 4.1, "lrecon": 4.0, "galtan": 5.3,
      "source_url": "https://www.chesdata.eu/..." },
    { "framework": "vparty", "framework_version": "V-Party_v3", "election_year": 2020,
      "v2pariglef_ord": 4, "v2paanteli_osp": 0.21, "v2paplur_osp": 0.65,
      "source_url": "https://www.v-dem.net/..." }
  ]
}
```

**Why the registry serves more questions than just the original one.** The same registry powers:

- "Which party shifted its rhetoric most between two elections?" — group speeches by party + cycle, mean-shift CMP score.
- "Did Politician X's rhetoric shift when they switched parties?" — per-politician CMP timeline against successive party affiliations.
- "Are members of Party Y converging or diverging in their rhetoric over time?" — within-party variance of CMP across cycles.
- "Which parties' parliamentary rhetoric most diverges from their manifestos?" — aggregate per-speech CMP per party-cycle, compare against the registry's CMP value for that cycle.

All reuse the same per-speech codings + the same party registry; no schema additions.

### 7. Aggregation methodology — documented canonical queries, not stored rollups

**Decision.** A small set of named, version-pinned query patterns is documented in `docs/canonical-queries.md`. Every published finding cites the query name and version (e.g., `most_populist_politicians@v1`). Per-speech codings remain the only persistence layer; consumer-defined custom queries are encouraged for one-off analyses but always disclaim the methodology in their reports.

**Why documented canonical queries.** Without them, the corpus becomes uniteable: every news article publishes a different ranking, every researcher disagrees on inclusion thresholds, the schema's value as research infrastructure collapses, and the legal posture weakens (no "we used the project's documented query, here's the spec" defense). Documenting canonical queries is the cheapest, highest-leverage methodology investment. They live in a docs file, they don't change the schema, they don't bloat any sidecar, and they make every published finding reproducible and citable.

**The six methodology choices each documented query must specify.**

1. **Inclusion threshold** — minimum codable speeches and minimum word count for a politician to qualify for headline ranking. **Default: ≥10 codable speeches AND ≥5,000 words** in the time window. Below threshold, a politician is reported in a separate "low-evidence" tier with the count, never in the headline ranking. (Hawkins's published-paper convention for the Global Populism Database is similar: leaders need a minimum number of speeches before a populism score is published.) Without a threshold, politicians with one or two speeches dominate the top of "% populist" rankings by accident — a single highly-populist intervention puts them at 100 %.
2. **Normalisation basis** — three sensible bases produce three different rankings:
   - Per-speech rate (`markers / speeches`) — penalises long careers.
   - Per-1000-words rate (`markers / kwords`) — closer to "discourse density".
   - Per-floor-time rate (`markers / minutes_speaking`) — cleanest accountability proxy but requires speech-duration metadata, which the canonical does not yet carry.
   **Default: per-1000-words as primary, per-speech as secondary, defer floor-time until duration metadata is added.** Document both; never collapse to one number without the basis annotated.
3. **Aggregation function (within a politician × time window)** — four sensible choices:
   - Mean of per-speech scores — sensitive to one-off extreme speeches.
   - Median — robust to outliers but loses signal from rare-extreme speakers.
   - 75th percentile — captures "how extreme are this politician's most extreme speeches", useful for episodic patterns like fearmongering.
   - Sum of marker counts (after normalisation) — intensity-weighted volume.
   **Default: report mean + 75th percentile + sum side by side.** Different rhetorical patterns peak under different aggregations; a politician who gives 10 wildly-populist speeches and 90 boring ones ranks differently under mean vs 75th percentile, and that difference is journalistically meaningful.
4. **Confidence-filtered vs all-codings rankings** — including low-confidence codings makes rankings noisier; excluding them biases toward politicians whose rhetoric is *easier to classify* (LLMs are more confident on stereotypical populism than on subtle dog-whistles, so subtle-rhetoric politicians get under-counted). **Default: publish two parallel rankings — high-confidence (`framework_confidence > 0.7 AND voice_confidence > 0.7`) and all-codings (no filter).** Movements between the two rankings are themselves journalistically meaningful.
5. **First-person vs deniable-wrapper rhetoric** — from Q5: voice is persisted per marker. **Default: always report two parallel rankings — first-person (`voice = speaker_first_person`) and deniable-wrapper (queries against `rhetorical_moves` bucket).** They are different rhetorical strategies; a politician high on first-person populism is open about it, a politician high on deniable-wrapper rhetoric operates with plausible deniability. Conflating them produces ranking artifacts that consistently flatter the more cunning operators.
6. **Stability — bootstrap confidence intervals on rank.** A politician at rank 3 with 12 speeches and another at rank 4 with 200 speeches are not equivalent. **Default: bootstrap 1,000 iterations, report rank 95 % CI.** A politician whose 95 % CI for rank is `[2, 14]` is not "rank 3"; they are "somewhere in the top-15 cluster." This single discipline prevents the most common journalism-misuse pattern of treating LLM-coded rankings as more precise than they are.

**Time-window granularities.** Canonical queries are published at three granularities:

- Calendar year (matches journalist phrasing; "most populist senator in 2020").
- Per legislature (Roman-numeralled in canonical metadata; matches political-science convention).
- Rolling 12-month (smooth view; trend lines).

Per-session (~6 months) is too short and noisy; not a default granularity.

**Concrete query record format.**

```
Query: most_populist_politicians@v1
Description: Rank politicians by Hawkins-graded populism density.
Inputs: time_window (e.g., 2020), chamber (Camera|Senate|both)
Filters:
  - speeches: speaker.party_group resolves (via party-registry alias) to a registered party
  - speeches: length_words >= 100
  - codings: framework=hawkins_populism, framework_confidence > 0.7, voice=speaker_first_person
  - politicians: >= 10 codable speeches AND >= 5000 words in window
Score:
  - per-1000-words rate of (hawkins_populism.score >= 1) markers
  - 75th percentile of per-speech populism scores
  - sum of populism markers
Output:
  - top 20 by per-1000-words rate
  - bootstrap 95% CI on rank, 1000 iterations
  - parallel "low-evidence" tier for excluded politicians
  - parallel "deniable-wrapper" ranking from rhetorical_moves
```

**Why not stored per-politician rollups.** See Q2 — they go stale, they couple the schema to one query definition, they invite libel framing. Documented queries achieve the same reproducibility without these costs.

### 8. Validation — hand-coded gold-standard sample plus cross-reference against published expert codings

**Decision.** Two layers of validation, both run as part of the project:

- **Hand-coded gold-standard sample** of 200 speeches stratified across populist / moderate / register-shifting politicians plus a random sub-sample. Two Romanian-fluent coders, blind to LLM output. Cohen's κ computed pairwise (coder1↔coder2, LLM↔coder1, LLM↔coder2). Re-run on every classifier upgrade.
- **Cross-reference against published expert codings** — aggregate per-speech CMP/CHES/V-Party codings to party-year level and check against the registry's expert codings. Aggregation-level agreement is an automatic CI signal on classifier drift.

Validation data lives in `validation/` (committed to repo): `gold_sample.jsonl` with hand-codings, `agreement_report.md` with κ tables.

**Why hand-coded gold sample.** No serious political-discourse paper will cite the corpus without inter-coder agreement statistics. Hawkins's published holistic-grading reports κ ≈ 0.7 — that is the bar to clear. A 200-speech hand-coded sample with two coders is a one-time investment of roughly 80 person-hours; tiny compared to the corpus value and necessary for the corpus to be academically citable.

**Sample composition** (200 speeches stratified):

- 5 politicians known to be populist (Vadim Tudor, Diana Șoșoacă, Călin Georgescu, Ringo Dămureanu, others) — should code high on Hawkins.
- 5 politicians known to be moderate-establishment (Klaus Iohannis-era PNL, Cioloș, others) — should code low on Hawkins.
- 5 politicians known to switch register across audiences (Băsescu, Ponta, others) — should show variance.
- ~150 random across years/parties/topics — for general κ representativeness.

Stratification ensures the validation covers the rhetorical spectrum, not only the easy mid-cases.

**Statistics computed.**

- Coder1 vs Coder2 κ — establishes the *human* inter-coder ceiling for this dataset.
- LLM vs Coder1 κ, LLM vs Coder2 κ — measures classifier agreement against humans.
- LLM agreement should approach (but not exceed) human-human κ. If it exceeds, the LLM is overfitting to surface cues humans do not fixate on; if it is much lower, the classifier needs work.
- Per-framework, per-axis. Do **not** compute one κ. Compute κ separately for: Hawkins-grade, V-Party-anti-pluralism, V-Dem each-attack-target, each rhetorical-act marker (insults, fearmongering, victimhood, fallacies). Some axes will be high-agreement (insults: easy); others will be low (sarcasm: hard). Reporting separately tells consumers which axes to trust.
- **Voice-attribution κ is its own bucket** — quote-vs-claim accuracy needs separate measurement: hand-code 100 speeches for marker voice; measure LLM voice-correctness. This is the highest-failure-risk dimension and should have its own κ tracked over time.

**CI integration.** Re-run agreement on every classifier upgrade. If κ drops on any axis, the upgrade is rejected.

**Cross-reference checks.** For each external framework, aggregate the corpus's per-speech codings to party-year level and compare against the registry:

- CMP: aggregate per-speech `cmp_left_right.score` per party-year, compare against `registries/party_positions.json` CMP entry for that party-year. Report Spearman correlation; flag if < 0.6.
- V-Party anti-pluralism: aggregate per-speech `vparty_anti_pluralism.score` per party-cycle, compare against V-Party `v2paplur_osp` for that party-election. Report correlation.
- CHES GAL-TAN: same shape.

These checks run automatically; they cost nothing once the registry exists; they catch classifier drift that per-speech κ might miss.

**Why not validation-free.** Forfeits academic-credibility ceiling, breaks legal defensibility, turns the corpus into tabloid-fodder.

**Why not gold-sample only.** Misses the cross-reference signal; cannot detect aggregation-level drift that cancels out per-speech.

**Why not cross-reference only.** Aggregation-level agreement can mask per-speech failures (party averages can be right while per-speech codings are randomly wrong as long as errors cancel). And no published cross-reference exists for many axes (no expert-coded ground truth for "fearmongering Romanian senator" exists).

### 9. Model choice — hybrid pipeline with frontier escalation and version-pinned prompts

**Decision.** A four-tier pipeline:

1. **Regex + dictionary** for high-volume, deterministic detection (insult dictionary lookup, quote-introducer phrases, negation cues, apophasis templates, named-target detection). Zero LLM cost.
2. **Smaller / cheaper LLM (Haiku-class or Romanian-tuned 7–13B)** for first-pass marker detection and first-pass voice classification, given the framework rubric in-context.
3. **Frontier LLM (Sonnet/Opus class)** for low-confidence escalation, voice-ambiguous cases, suspected sarcasm, dense rhetorical-moves, contested re-coding.
4. **Human review** for the validation gold-sample only.

All prompts are version-pinned and stored in `prompts/`. No silent prompt drift.

**Why hybrid.**

1. **Cost containment.** ~1,200 corpus PDFs × ~50 codable speeches each × 8 framework codings = roughly 480k LLM calls per full corpus pass. At frontier-model rates (~$3–15 per 1k speech-codings), that is $1,500–7,500 per full re-coding. With classifier iteration (the corpus will be re-coded 10+ times during development), the bill compounds fast. The hybrid drops cost 5–10× by routing easy cases to cheap models and reserving frontier calls for hard ones.
2. **Deterministic regex catches ~40–60 %.** Insult dictionaries (Romanian-language list of slurs and pejoratives, pre-seeded and grown), named-target detection (cross-referencing speakers and party names already in the canonical), quote-introducer phrases (`citez:`, `X susține că`, `nu este adevărat că`), apophasis cues (`nu spun că ... dar`), explicit fearmongering vocabulary (`pericol existențial`, `invazie`, `sfârșitul poporului român`) — all regex-detectable. Dictionary-based detection of obvious markers at zero LLM cost handles a large share of cases.
3. **Smaller model handles first-pass.** A Romanian-fluent open-source 7–13B (or Haiku) handles "is this speech populist? rate 0/1/2 with markers" decently when given the framework rubric in-context. Use this as the first-pass classifier for every speech; reserve frontier escalation for the cases where it is needed.
4. **Frontier model for the hard cases.** When the small model has low confidence, when voice attribution is ambiguous, when sarcasm is suspected, when rhetorical-moves are dense, escalate to a frontier model. Keep the escalation rate under 20 % to keep costs manageable.
5. **Frontier-model Romanian competence is real.** Frontier multilingual LLMs handle Romanian political discourse well — Băsescu's specific register, AUR's irredentist vocabulary, post-1989 anti-communist tropes, regional dialects, and parliamentary-procedure terms-of-art are all within reach.

**Why translation pre-pass is the worst option.** Romanian political language is full of register shifts (formal *Dumneavoastră* vs populist tu-form), idioms (`a băga capul în nisip`, `a face din țânțar armăsar`, `cu mâna pe inimă`), historical references (`legionar`, `comunisto-securist`, `traseism`, `securism`, `băsism`), region-specific slurs, religious-nationalist framing, and parliamentary-procedure terms-of-art that do not translate cleanly. Translation adds an error layer between the source and the classifier; it should be avoided.

**Implementation discipline.**

- **Prompts are version-pinned and stored in the repo** under `prompts/`: one prompt file per framework / per phenomenon — `hawkins_populism_v1.md`, `dqi_v1.md`, `vparty_anti_pluralism_v1.md`, `cmp_rile_v1.md`, `ches_galtan_v1.md`, `vdem_attacks_v1.md`, `voice_classifier_v1.md`, `rhetorical_moves_v1.md`, `securitization_v1.md`, `conspiracy_v1.md`, `apologia_v1.md`, `geopol_v1.md`, `gendered_v1.md`, `climate_v1.md`, `pandemic_v1.md`, `insults_v1.md`, `fearmongering_v1.md`, `victimhood_v1.md`, `fallacies_v1.md`, `romanian_specific_v2.md`. Re-running with a new prompt version is a `prompt_version` bump in the analysis sidecar's `extractor_versions` block. **No silent prompt drift.**
- **Each framework's prompt includes the full rubric inline** (Hawkins's 7-marker explanation, V-Party's anti-pluralism definition, CMP's category list with the 56 RILE-relevant categories). Do not rely on the LLM having read the academic paper.
- **All LLM calls are batched and cached.** Speech-text → SHA → coding cache. Re-runs over the same speech with the same prompt version hit cache. No re-classifying speeches whose source `content_sha` has not changed.
- **Few-shot examples per framework** are drawn from the gold-coded sample (Q8). Provides the classifier with concrete Romanian-language examples of each marker.
- **No fine-tuning in v1.** Premature optimisation. Few-shot plus good prompts get further than fine-tuning until the gold sample is much larger than 200 speeches.

**Concrete model assignment.**

| Step | Model | Rationale |
|---|---|---|
| Quote/report/negation region detection | Regex + small model fallback | High-volume, regex catches most |
| Insult dictionary lookup | Regex (custom Romanian dictionary, version-pinned) | Deterministic, exhaustive |
| Marker detection (first pass, all frameworks) | Haiku-class or open-source 7–13B | Volume-tolerant, decent on rubric-following |
| Voice classification (per marker) | Same first-pass model; escalate on uncertainty | Most cases are easy |
| Rhetorical-move detection (apophasis, dog-whistles, sarcasm) | Frontier model (Sonnet/Opus class) | Subtle, context-dependent |
| Contested / low-confidence reclassification | Frontier model | Quality matters more than cost on hard cases |
| Validation gold sample | Human coders | Ground truth |

### 10. Storage layout and multi-rubric overlay — one sidecar per document, per-framework versioning inside

**Decision.** One `.analysis.json` per canonical document, all framework codings inside, each framework's coding carrying its own `framework_version` and `prompt_version`. Upgrading one framework rewrites that subtree of the sidecar; the file remains one file per document.

**Why one file per document.**

- **Per-framework iteration is the dominant maintenance pattern.** When CHES 2028 publishes, only CHES codings re-run. With per-framework files, that is clean — one file. With one file containing all frameworks but per-framework versions inside, the same atomicity is achieved (rewrite the subtree) without the file-count explosion.
- **Joinability.** The whole point of the analysis sidecar is to be joinable to the canonical at the document level. Multiple per-framework files complicate the join without adding value (consumers query both anyway). One file per document, per-framework versioning inside, gets the best of both worlds.
- **File count.** ~1,200 corpus docs × 1 sidecar each = 1,200 sidecars. Manageable. ~1,200 × 8 frameworks would be ~10,000 sidecars — operationally noisier (S3 PUTs, filesystem listings, ls performance, atomic-rename coordination across multiple files).
- **Atomic write.** Per-document writes are straightforward: write to `<basename>.analysis.json.tmp`, fsync, rename. Same atomic pattern as PDF and MD writes in the existing pipeline. Per-framework files would either accept partial state or require multi-file coordination.

**Concrete file layout.**

```
pdfs/
  2026-04-29_MO-PII-48-2026.pdf
  2026-04-29_MO-PII-48-2026.md
  2026-04-29_MO-PII-48-2026.json              # canonical (schema_version 1.4.0)
  2026-04-29_MO-PII-48-2026.analysis.json     # discourse layer (analysis_version 0.2.0)
registries/
  party_positions.json                         # Q6's party-position registry
  insult_dictionary.ro.json                    # custom Romanian insult / pejorative dictionary
  framework_marker_definitions.json            # closed marker sets per framework
  romanian_specific_markers.json               # Romania-specific marker definitions + examples
  conspiracy_markers.json                      # conspiracy-framing markers + examples (statul paralel, Soros, etc.)
  geopol_alignment_markers.json                # geopolitical-alignment markers + examples
prompts/
  hawkins_populism_v1.md
  dqi_v1.md
  vparty_anti_pluralism_v1.md
  cmp_rile_v1.md
  ches_galtan_v1.md
  vdem_attacks_v1.md
  voice_classifier_v1.md
  rhetorical_moves_v1.md
  insults_v1.md
  fearmongering_v1.md
  victimhood_v1.md
  fallacies_v1.md
  securitization_v1.md
  conspiracy_v1.md
  apologia_v1.md
  geopol_v1.md
  gendered_v1.md
  climate_v1.md
  pandemic_v1.md
  romanian_specific_v2.md
docs/
  canonical-queries.md                         # Q7's documented query patterns
validation/
  gold_sample.jsonl                            # Q8's hand-coded validation set
  agreement_report.md                          # κ statistics per framework, per coder pair, per LLM version
```

**Sidecar shape.** See the consolidated schema reference below for the full envelope and per-coding record. Q10's contribution is the *one-file-per-document* decision plus the per-framework versioning discipline; the literal shape lives once, in the consolidated reference, to avoid two-source-of-truth drift.

### 11. Pipeline integration — `analyze` subcommand mirroring `convert`

**Decision.** A new CLI subcommand `monitorul-ii analyze` alongside `fetch` and `convert`. Same idempotency / progress-bar / parallelism patterns as `convert`. SQLite audit DB is extended with an `analyses` table mirroring the existing `issues` table. S3 mirror for `.analysis.json` mirrors the PDF/MD pattern.

**CLI signature** (proposed):

```
monitorul-ii analyze <path> [<path> ...]
  [--frameworks F[,F...]]    # default: all; e.g., hawkins,vparty,dqi
  [--force]                  # re-run even when up-to-date
  [-j N | --workers N]
  [--reverse]
  [--bucket NAME | --no-upload]
  [--db PATH | --no-db]
  [--budget USD]             # hard ceiling on LLM spend; halts gracefully when reached
  [--min-words N]            # skip speeches below this word floor (default 50);
                             #   below the floor the speech is recorded as `skipped_short`
                             #   in the sidecar so consumers can audit coverage
  [--dry-run]                # estimate cost/runtime without LLM calls
```

**Idempotency.** Skip a doc when:

- `<basename>.analysis.json` exists, AND
- its `target_canonical_sha` equals the canonical's current `content_sha`, AND
- its `extractor_versions` block matches the current configured versions for every framework being run.

Otherwise re-run. Per-framework granularity: if Hawkins succeeds and V-Party fails on a doc, the partial sidecar persists with Hawkins populated and V-Party absent; the failed framework retries on the next run.

**Pipeline order.** `analyze` requires the canonical `.json` to exist. It cannot run on bare `.md` because it needs the per-speech segmentation that the canonical extractor produces. The chain is:

```
fetch  →  convert  →  extract  →  analyze
```

`extract` is the future canonical extractor (build order steps 1–7 in `docs/extraction-schema.md`). `analyze` is the next stage in this chain.

**S3 mirror.** `.analysis.json` files mirror to S3 with `Content-Type: application/json`, same flat-key pattern as PDF/MD. Same `head_object`-then-upload-if-missing idempotency. Same `--bucket` / `--no-upload` switches.

**DB layer — new `analyses` table:**

```sql
CREATE TABLE analyses (
  document_id TEXT PRIMARY KEY,             -- mo://YYYY/PII/NN
  analysis_version TEXT,
  extractor_versions_json TEXT,             -- JSON blob; matched to current to decide skip/run
  target_canonical_sha TEXT,
  status TEXT,                              -- pending | running | ok | failed | partial
  frameworks_completed TEXT,                -- comma-joined names
  llm_tokens_in INTEGER,
  llm_tokens_out INTEGER,
  llm_cost_usd REAL,
  completed_at TIMESTAMP,
  last_error TEXT,
  attempts INTEGER DEFAULT 0
);
```

Mirrors the `issues` table pattern. Resume gate: skip docs in `ok` status with matching versions; retry `failed` and `partial`. Cost telemetry lets `--budget` enforce ceilings.

**Cost telemetry.** Every LLM call is logged with `(framework, model, tokens_in, tokens_out, cost_usd, document_id, target_path)`. Aggregated into the DB row for the document. Per-run cost summary printed at the end. Quarterly cost report from the DB.

**Why a subcommand and not a separate script.** The existing pipeline architecture is already the right shape (CLI subcommands, idempotent, DB-gated, progress-bar, S3 mirror). Adding `analyze` as a subcommand extends the pattern; building a parallel scripts directory bifurcates it.

**Why per-framework granular idempotency.** Frameworks fail independently (rate limits, transient API errors, model outages). Treating the sidecar as "all-or-nothing" forces the whole document to be re-run on any single-framework failure; per-framework granularity keeps the work that succeeded.

**Why `--budget`.** Discourse classification will run repeatedly during development; without a hard ceiling, an inadvertent loop or unexpectedly-large input can produce a five-figure bill. `--budget` is a hard `max_spend` argument; the run halts gracefully when reached, persists what completed, exits non-zero. Belt-and-suspenders alongside per-call cost logging.

**Why `--min-words`.** Coding 30-word interjections wastes LLM spend and inflates per-speech populism rates with noise — a single populist trope in a 30-word floor-time grab counts the same as one in a 4,000-word programmatic speech, distorting per-speech-rate aggregations. The floor is a configurable per-speech eligibility gate, not a query-time filter, because skipping below the floor is also a cost discipline. `skipped_short` is recorded so consumers can audit coverage and re-run with a lower floor if their query needs it.

### 12. Audience-reaction signals — join from canonical narrator events

**Decision.** Each per-speech analysis record carries an `audience_signals` block summarising the canonical's narrator events that occurred *during* the speech. No LLM cost; pure join over the canonical's existing `narrator` activities (`(Aplauze.)`, `(Rumoare.)`, `(I se întrerupe microfonul.)`, walkout / banner / mic-cut events). Audience signals are evidence on the speech, not a coding *of* the speech, and live alongside `framework_codings` and `custom_codings` rather than under either.

**Why this is load-bearing despite being free.** The most defensible polarisation signal in the corpus is sitting in the canonical and the v0.1 analysis layer ignored it. Sustained applause from one party group plus heckling, walkout, or mic-cut from another is *cross-aisle disruption*, evidenced by the chair's stenographic record — no LLM judgment, no κ measurement, no defamation surface beyond what the canonical already exposes. The canonical's planned `narrator.kind` enum (`extraction-schema.md` v1.4 backfill: `applause | murmur | silence | protest | walkout | mic_cut | request_floor | speaker_arrives | speaker_leaves | anthem | other`) is the natural input. Per-speech aggregation is a four-line transformation.

**Block shape.**

```json
"audience_signals": {
  "applause_count": 2,
  "murmur_count": 1,
  "interruption_count": 4,
  "heckling_count": 2,
  "walkout_during_speech": false,
  "mic_cut": false,
  "banner_displayed": false,
  "anthem_played": false,
  "narrator_events": [
    { "kind": "applause", "source_span": SourceSpan, "verbatim": "(Aplauze.)" },
    { "kind": "interruption", "source_span": SourceSpan, "verbatim": "(Domnul X intervine din sală.)" }
  ]
}
```

**Per-speech polarisation as a query, not a stored field.** Same logic as Q4 (no stored composites): a "polarisation index" depends on definition (cross-party-group applause variance? walkout-by-other-party? mic-cut?). Store the components, derive the index at query time. Documented queries in `canonical-queries.md` codify whichever definition the project canonicalises.

**Why not store the verbatim narrator text only and derive counts at query time.** Same reason canonical's flat projections are derived at ES-ingest, not stored: counts are cheap to compute *once*, expensive to recompute per query, and benefit from being aggregated alongside codings for filter/rank queries. The verbatim text is also persisted for audit (`narrator_events[]`), so nothing is lost.

**Why this is not a "framework coding".** Audience signals are evidence about the speech's *reception*, not a coding of its *content*. A speech can be substantively populist and receive no notable audience reaction; another can be procedurally innocuous and produce a walkout. They belong on different conceptual axes. Keeping them in their own block prevents conflating "high audience disruption" with "high populism".

**Backfill compatibility.** When the canonical's `narrator.kind` enum is added (currently planned for v1.4), the analysis-layer join becomes deterministic. Until then, the join uses regex over verbatim narrator text (`Aplauze`, `Rumoare`, `Se întrerupe microfonul`, `pancarte`, etc.). Coverage is good — the narrator strings in the corpus are stenographer-templated and highly regular.

### 13. Build order — sequence the work for incremental shipping

Each step ships value independently. The keystone prerequisites are out of order because they unblock everything else.

1. **Document `docs/canonical-queries.md`.** Initial queries: `most_populist@v1`, `most_anti_pluralist@v1`, `highest_dqi@v1` (positive deliberative-quality ranking), `cross_axis_divergence@v1` (the "left politician with right-wing speech" query), `most_fearmongering@v1`, `rhetorical_fallacy_density@v1`, `most_securitizing@v1`, `most_conspiracy_framing@v1`, `most_pro_russia@v1` (post-2022 windowed), `audience_disruption_index@v1`. Names and inclusion rules pinned before any code is written. Cheap, high-leverage, unblocks everything downstream.
2. **Bootstrap the party-position registry.** `registries/party_positions.json`. Hand-curate from CHES-2024 + Manifesto Project most-recent + V-Party most-recent for the ~12 active Romanian parties. No automation needed for this size. Ship as a separate change; no schema or pipeline coupling.
3. **Build the canonical extractor.** Steps 1–7 from `docs/extraction-schema.md`'s build order. This is the prerequisite for everything else and is its own multi-week effort. Without per-speech canonical records, there is nothing to analyse.
4. **Hand-code the gold-standard sample (200 speeches, 2 coders).** Run in parallel with (3); it is coder-time, not engineer-time. Result: `validation/gold_sample.jsonl`. Establishes the human-κ ceiling.
5. **Build voice classifier first.** Voice attribution is the load-bearing failure mode (Q5). Get it working before any framework coding. Validate against the gold sample's voice annotations.
6. **Audience-signals join (Q12).** Pure regex / enum-based pass over canonical narrator events; no LLM cost. Ships before any framework coding because it has no LLM-classifier dependency and unlocks the audience-disruption query class immediately. Backfilled when the canonical's `narrator.kind` enum lands.
7. **Hawkins populism classifier.** Single framework, simplest rubric (0/1/2 with 7 markers), best published baseline (Global Populism Database has Romanian leaders coded for cross-reference). Ship as a working `analyze --frameworks hawkins` end-to-end before touching V-Party / CMP / CHES.
8. **DQI deliberative-quality classifier.** Ship right after Hawkins. Establishes the positive axis the layer would otherwise lack and is the highest-credibility published-framework upgrade. Six sub-codings per speech; same shape as Hawkins but multi-dimensional.
9. **`analyze` subcommand + DB + S3 mirror.** Once Hawkins + DQI work for one doc, integrate into the pipeline. Idempotency, per-framework granularity, progress bar, cost telemetry, `--budget`, `--min-words` floor.
10. **Add V-Party anti-pluralism, then CHES, then CMP frameworks.** Sequential, one PR each. Each framework adds a new key to `framework_codings` and a new prompt file. Each independently validated against gold sample.
11. **V-Dem attacks-on-X markers.** Conceptually simpler than the framework-anchored axes (per-target counts, no aggregate score). Ship after the core frameworks.
12. **Conspiracy-framing custom bucket.** High Romanian-context value (`statul paralel`, Soros, globalist-cabal). Anchored to Uscinski–Parent. Ship before the other custom buckets because of corpus-specific salience.
13. **Securitization markers.** Distinct from fearmongering; predicts authoritarian-tendency outcomes. Ship after conspiracy-framing.
14. **Custom buckets, in order:** `insults` → `fearmongering` → `victimhood` → `fallacies` → `apologia` → `gendered_rhetoric` → `geopolitical_alignment` → `pandemic_health` → `climate_denial` → `rhetorical_moves` → `romanian_specific`. Each independently versioned. The split-out shape (per-phenomenon `rubric_version`) replaces the v0.1 lumped `rhetorical_acts` bucket.
15. **Cross-axis divergence query.** Once both per-speech CHES/CMP codings and party-position registry are in place, `cross_axis_divergence@v1` becomes a documented query. No additional schema.
16. **Aggregation reports (canonical-query CLI).** Documented canonical queries (`most_populist@v1` etc.) ship as standalone scripts (`monitorul-ii report most-populist --year 2020 --chamber camera`). Idempotent over the analysis sidecars.
17. **(Later)** Re-validate κ on every classifier upgrade. CI integration.
18. **(Later)** ES ingest of the analysis layer. Same as canonical: hierarchical → flat row indices for ranked queries.
19. **(Later)** Public dashboard / API. Out of schema scope; uses ES.

## Consolidated schema reference

### Sidecar envelope

```json
{
  "analysis_version": "0.2.0",
  "target_document": "mo://2026/PII/48",
  "target_canonical_sha": "a3f9c1d2e4b8",
  "extracted_at": "2026-05-04T10:30:00Z",
  "extractor_versions": {
    "frameworks": {
      "hawkins_populism":     { "rubric_version": "hawkins@2018",            "prompt_version": "hawkins_v1", "model": "claude-sonnet-4-6" },
      "dqi":                  { "rubric_version": "steiner-bachtiger@2017",  "prompt_version": "dqi_v1",     "model": "claude-sonnet-4-6" },
      "vparty_anti_pluralism":{ "rubric_version": "vparty@v3",               "prompt_version": "vparty_v1",  "model": "claude-sonnet-4-6" },
      "cmp_left_right":       { "rubric_version": "manifesto-project@2024-1","prompt_version": "cmp_v1",     "model": "claude-sonnet-4-6" },
      "ches_gal_tan":         { "rubric_version": "ches@2024",               "prompt_version": "ches_v1",    "model": "claude-sonnet-4-6" },
      "vdem_attacks":         { "rubric_version": "vdem@v15-attacks",        "prompt_version": "vdem_v1",    "model": "claude-sonnet-4-6" }
    },
    "custom": {
      "insults":               { "rubric_version": "custom-insults@0.1",       "prompt_version": "insults_v1",       "model": "claude-sonnet-4-6" },
      "fearmongering":         { "rubric_version": "custom-fearmongering@0.1", "prompt_version": "fearmongering_v1", "model": "claude-sonnet-4-6" },
      "victimhood":            { "rubric_version": "custom-victimhood@0.1",    "prompt_version": "victimhood_v1",    "model": "claude-sonnet-4-6" },
      "fallacies":             { "rubric_version": "custom-fallacies@0.1",     "prompt_version": "fallacies_v1",     "model": "claude-sonnet-4-6" },
      "rhetorical_moves":      { "rubric_version": "custom-rhet-moves@0.1",   "prompt_version": "rhet_moves_v1",    "model": "claude-sonnet-4-6" },
      "securitization":        { "rubric_version": "custom-securitization@0.1","prompt_version": "securitization_v1","model": "claude-sonnet-4-6" },
      "conspiracy_framing":    { "rubric_version": "custom-conspiracy@0.1",   "prompt_version": "conspiracy_v1",    "model": "claude-sonnet-4-6" },
      "apologia":              { "rubric_version": "custom-apologia@0.1",     "prompt_version": "apologia_v1",      "model": "claude-sonnet-4-6" },
      "geopolitical_alignment":{ "rubric_version": "custom-geopol@0.1",       "prompt_version": "geopol_v1",        "model": "claude-sonnet-4-6" },
      "gendered_rhetoric":     { "rubric_version": "custom-gendered@0.1",     "prompt_version": "gendered_v1",      "model": "claude-sonnet-4-6" },
      "climate_denial":        { "rubric_version": "custom-climate@0.1",      "prompt_version": "climate_v1",       "model": "claude-sonnet-4-6" },
      "pandemic_health":       { "rubric_version": "custom-pandemic@0.1",     "prompt_version": "pandemic_v1",      "model": "claude-sonnet-4-6" },
      "romanian_specific":     { "rubric_version": "custom-ro@0.2",           "prompt_version": "ro_specific_v2",   "model": "claude-sonnet-4-6" }
    },
    "voice_classifier":  { "version": "voice@0.1",  "prompt_version": "voice_v1",  "model": "claude-haiku-4-5" },
    "audience_signals":  { "version": "audience@0.1", "method": "regex+narrator-enum", "model": null }
  },
  "cost_telemetry": {
    "llm_tokens_in":  482311,
    "llm_tokens_out":  41209,
    "llm_cost_usd":    1.37,
    "speeches_skipped_short": 14
  },
  "codings": [ /* one per speech-act in the canonical document */ ]
}
```

### Per-coding record (one per speech-act)

```json
{
  "target_path": "mo://2026/PII/48#agenda/4/activity/7",
  "speaker_ref": {
    "raw": "Domnul Marian Crușoveanu",
    "name": "Marian Crușoveanu",
    "party_group": "PNL",
    "person_id": null
  },
  "speech_word_count": 412,
  "skipped_short": false,                          // true when below `--min-words` floor; codings absent
  "delivery_mode": "delivered",                    // delivered | written_interpellation | inserted_for_record

  "framework_codings": {
    "hawkins_populism": {
      "framework_version": "hawkins@2018",
      "score": 1,
      "score_unit": "ordinal_0_2",
      "framework_confidence": 0.78,
      "markers": [
        { "kind": "people_vs_elite",
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_evidence": null,
          "voice_confidence": 0.95,
          "attributed_to": null },
        { "kind": "moralistic_manichaeism",
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.91 }
      ],
      "rationale": "Speech frames a 'corrupt establishment' against 'the honest Romanian people' and...."
    },
    "dqi": {
      "framework_version": "steiner-bachtiger@2017",
      "score_unit": "dqi_multi",
      "framework_confidence": 0.73,
      "level_of_justification": 2,                  // 0=none, 1=inferior, 2=qualified, 3=sophisticated
      "content_of_justification": "common_good",    // group_interest | common_good | mixed | none
      "respect_for_groups": 1,                      // 0=disrespect, 1=neutral, 2=explicit respect
      "respect_for_demands": 1,
      "respect_for_counterarguments": 0,            // 0=ignored, 1=acknowledged, 2=engaged-with
      "constructive_politics": "alternative_proposal", // positional | alternative_proposal | mediating_proposal
      "markers": [
        { "kind": "level_of_justification",
          "value": 2,
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.96 }
      ],
      "rationale": "Speaker offers a qualified policy justification appealing to common-good criteria, proposes an alternative, but ignores opposition counterarguments raised in the prior turn."
    },
    "vparty_anti_pluralism": {
      "framework_version": "vparty@v3",
      "score": 0.62,
      "score_unit": "continuous_0_1",
      "framework_confidence": 0.71,
      "markers": [
        { "kind": "opposition_delegitimization",
          "evidence": SourceSpan,
          "target": "PSD",
          "voice": "speaker_first_person",
          "voice_confidence": 0.93 }
      ]
    },
    "cmp_left_right": {
      "framework_version": "manifesto-project@2024-1",
      "score": -7.4,
      "score_unit": "rile_econ",
      "framework_confidence": 0.65,
      "marker_counts": { "per501": 3, "per503": 2, "per401": 0, "per414": 1 }
    },
    "ches_gal_tan": {
      "framework_version": "ches@2024",
      "score": 7.2,
      "score_unit": "galtan_0_10",
      "framework_confidence": 0.69,
      "markers": [ /* CHES-relevant evidenced markers */ ]
    },
    "vdem_attacks": {
      "framework_version": "vdem@v15-attacks",
      "judiciary": 0,
      "opposition": 1,
      "media": 0,
      "minorities": 0,
      "civil_society": 0,
      "framework_confidence": 0.74,
      "markers": [
        { "target": "opposition",
          "kind": "delegitimization",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    }
  },

  "custom_codings": {
    "insults": {
      "framework_version": "custom-insults@0.1",
      "items": [
        { "term": "trădătorul",
          "target": { "name": "Liviu Dragnea", "kind": "individual" },
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.97 }
      ]
    },
    "fearmongering": {
      "framework_version": "custom-fearmongering@0.1",
      "markers": [
        { "kind": "existential_threat",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "victimhood": {
      "framework_version": "custom-victimhood@0.1",
      "markers": [
        { "kind": "we_are_victims",
          "in_group": "românii cinstiți",
          "out_group": "elitele corupte",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "fallacies": {
      "framework_version": "custom-fallacies@0.1",
      "markers": [
        { "kind": "false_dichotomy",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" },
        { "kind": "ad_hominem",
          "target": "Liviu Dragnea",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "rhetorical_moves": {
      "framework_version": "custom-rhet-moves@0.1",
      "moves": [
        { "kind": "apophasis",
          "evidence": SourceSpan,
          "literal_voice": "negated",
          "implied_content_summary": "claim that Hungarians threaten Transylvania",
          "confidence": 0.74 },
        { "kind": "weasel_attribution",
          "evidence": SourceSpan,
          "literal_voice": "attributed",
          "weasel_phrase": "unii spun că",
          "implied_content_summary": "the EU is dictating Romanian policy",
          "confidence": 0.81 },
        { "kind": "just_asking_questions",
          "evidence": SourceSpan,
          "literal_voice": "interrogative",
          "implied_content_summary": "vaccine safety is being hidden by authorities",
          "confidence": 0.69 }
      ]
    },
    "securitization": {
      "framework_version": "custom-securitization@0.1",
      "securitization_move_present": true,           // co-occurrence: existential + referent + extraordinary measures
      "markers": [
        { "kind": "existential_framing",
          "referent_object": "civilizația europeană creștină",
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.92 },
        { "kind": "extraordinary_measures_invoked",
          "measure_kind": "border_emergency",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "conspiracy_framing": {
      "framework_version": "custom-conspiracy@0.1",
      "markers": [
        { "kind": "parallel_state",
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.94 },
        { "kind": "soros_conspiracy",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "apologia": {
      "framework_version": "custom-apologia@0.1",
      "markers": [
        { "kind": "differentiation",
          "accusation_referent": "DNA file no. 123/2018",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    },
    "geopolitical_alignment": {
      "framework_version": "custom-geopol@0.1",
      "markers": [
        { "kind": "nato_skeptic",
          "evidence": SourceSpan,
          "voice": "speaker_first_person",
          "voice_confidence": 0.81 }
      ]
    },
    "gendered_rhetoric": {
      "framework_version": "custom-gendered@0.1",
      "markers": []
    },
    "climate_denial": {
      "framework_version": "custom-climate@0.1",
      "markers": []
    },
    "pandemic_health": {
      "framework_version": "custom-pandemic@0.1",
      "markers": []
    },
    "romanian_specific": {
      "framework_version": "custom-ro@0.2",
      "markers": [
        { "kind": "anti_justice_reform",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" },
        { "kind": "country_for_sale",
          "evidence": SourceSpan,
          "voice": "speaker_first_person" }
      ]
    }
  },

  "audience_signals": {
    "applause_count": 2,
    "murmur_count": 1,
    "interruption_count": 4,
    "heckling_count": 2,
    "walkout_during_speech": false,
    "mic_cut": false,
    "banner_displayed": false,
    "anthem_played": false,
    "narrator_events": [
      { "kind": "applause",     "source_span": SourceSpan, "verbatim": "(Aplauze.)" },
      { "kind": "interruption", "source_span": SourceSpan, "verbatim": "(Domnul X intervine din sală.)" }
    ]
  },

  "extraction": PerSectionExtraction
}
```

### Reused shapes (from canonical)

`SourceSpan` and `Speaker` shapes are inherited from `extraction-schema.md`. `SourceSpan` carries `lines`, `chars`, `content_sha`. `Speaker` carries `raw`, `name`, `title`, `role`, `party_group`, `person_id`.

`PerSectionExtraction` carries `extractor`, `confidence`, `source_span` — same shape as the canonical's per-section extraction block, with `extractor` formatted as e.g. `analysis-hybrid@0.1` rather than `regex@1`.

### Per-marker shape (recap)

Every marker — across every framework, framework-anchored or custom — carries:

```json
{
  "kind": "<framework-specific closed enum value>",
  "evidence": SourceSpan,
  "voice": "speaker_first_person | quoted | reported | negated | hypothetical | apophasis_disclaimed | weasel_attribution | sarcastic | interrogative | uncertain",
  "voice_evidence": SourceSpan | null,
  "voice_confidence": 0.0..1.0,
  "attributed_to": Speaker | null,             // populated when voice ∈ {quoted, reported}
  "target": "<optional, marker-kind-specific>", // e.g. for opposition_delegitimization, ad_hominem, insult
  "framework_confidence": 0.0..1.0,             // when applicable; some custom-rubric markers omit this in favour of just per-marker confidence
  "era_window": { "from": "YYYY-MM-DD", "to": "YYYY-MM-DD" | null } | null  // optional; warns on validation if speech date falls outside the marker's defined era
}
```

The voice + evidence + attribution columns are the audit-trail substrate that makes every coding contestable, citable, and defensible.

### Party-position registry shape

```json
{
  "registry_version": "0.1.0",
  "extracted_at": "2026-05-04T...",
  "parties": [
    {
      "id": "psd",
      "canonical_name": "Partidul Social Democrat",
      "aliases": ["PSD", "P.S.D.", "Partidul Social Democrat",
                  "Grupul parlamentar PSD", "Grupul parlamentar al PSD"],
      "active": { "from": "1993-07-10", "to": null },
      "successor_relations": [
        { "kind": "splinter_origin", "from": "fsdn_1992", "year": 1993 }
      ],
      "codings": [
        { "framework": "manifesto-project", "framework_version": "MARPOR_2024-1",
          "election_year": 2020, "rile": -8.2, "rile_econ": -12.4, "gal_tan": null,
          "source_url": "https://manifesto-project.wzb.eu/..." },
        { "framework": "ches", "framework_version": "CHES_2024", "wave_year": 2024,
          "lrgen": 4.1, "lrecon": 4.0, "galtan": 5.3,
          "source_url": "https://www.chesdata.eu/..." },
        { "framework": "vparty", "framework_version": "V-Party_v3", "election_year": 2020,
          "v2pariglef_ord": 4, "v2paanteli_osp": 0.21, "v2paplur_osp": 0.65,
          "source_url": "https://www.v-dem.net/..." }
      ]
    }
  ]
}
```

## Backfill paths

Like the canonical schema, this layer reserves slots that future registries fill in.

| Field | Registry / Source | When |
|---|---|---|
| `speaker_ref.person_id` | `persons` registry (same one referenced by canonical) | After canonical schema's person registry comes online |
| `marker.attributed_to.person_id` (when `voice ∈ {quoted, reported}`) | `persons` registry | Same time as canonical backfill |
| `cmp_left_right` party-aggregate cross-reference | `party_positions.json` registry | Always available (hand-curated) |
| `vparty_anti_pluralism` party-aggregate cross-reference | `party_positions.json` registry | Always available |
| `ches_gal_tan` party-aggregate cross-reference | `party_positions.json` registry | Always available |
| `insults.items[].target.person_id` | `persons` registry (when target is individual) | Same time as canonical backfill |
| `securitization.markers[].referent_object_id` | Future `referent_objects` controlled vocabulary (nation, sovereignty, civilisation, ethnic-group, family) | Once corpus reveals the recurrent referent set |
| `audience_signals.narrator_events[].kind` | Canonical `narrator.kind` enum (planned `extraction-schema.md` v1.4) | When canonical lands the enum; until then, regex-derived |
| `vdem_attacks.markers[].target_entity` (e.g. specific judge or media outlet) | Future `institutional_entities` registry | After enough corpus to identify recurrent targets |
| Cross-reference against external fact-checks | Future fact-check linker (Funky Citizens, Veridica, Factual.ro) | Out of v1 scope |

All backfills are mechanical column updates over stored JSON; no re-extraction of structure required, mirroring the canonical's backfill discipline.

## Versioning

Two version axes, deliberately independent (mirroring the canonical's `schema_version` ↔ `extraction.extractor` split):

- **`analysis_version`** — bumped when the *sidecar shape* changes. Major on breaking changes (field removed, type changed), minor on additive (new framework added, new marker kind), patch on clarifications. Migration is a script that reads all sidecars, transforms, writes back.
- **`extractor_versions.frameworks.<name>.{rubric_version, prompt_version, model}`** — per-framework iteration. Re-coding a framework is `WHERE rubric_version < ...` filesystem walk over sidecars, re-running only the relevant subtree.

These are independent because schema evolution and per-framework iteration evolve on different rhythms. Hawkins-rubric refinement, V-Party version updates, prompt rewording, model upgrades — none of these touch `analysis_version`. Adding a new framework to `framework_codings`, adding a new marker kind to a closed enum, removing a deprecated framework — these touch `analysis_version`.

## Risks and limitations (recorded for future readers)

1. **LLM hallucination on rationale.** The optional `rationale` field is LLM-generated text. It should never be cited as authoritative — only the `markers` and their `evidence` are auditable. Consumers must treat `rationale` as a debugging aid, not a finding.
2. **Voice-attribution precision is the highest single failure mode.** Even with two-pass extraction, sarcasm and complex apophasis remain hard. The validation gold sample's voice-κ tracks this; if it drops below 0.5, halt classifier upgrades and revisit the prompt and region detection.
3. **Romanian-specific markers risk false positives.** "Anti-Hungarian" rhetoric is contested at the edges (a debate about UDMR's seat count is not anti-Hungarian; a debate about ethnic-Hungarian schools may or may not be, depending on framing). The custom rubrics need particularly careful gold-sample validation and conservative thresholds. The expanded v0.2 catalogue (`antonescu_rehabilitation`, `holocaust_relativisation`, `parallel_state`, `country_for_sale`, `băsism`, `corectitudine_politica`, etc.) carries an even higher false-positive risk in historical-comparison or quoted-and-rejected contexts; voice tracking is the primary mitigation.
4. **Frontier-model deprecation.** Models named in `extractor_versions` will be deprecated by their providers eventually. The `model` field is informational; re-runs use whichever model is current. When a model deprecation is announced, schedule a corpus re-coding under the replacement.
5. **Defamation jurisdiction.** Romanian Civil Code articles 252–256 (right to dignity, image, private life) are the immediate concern; if the corpus is published under EU jurisdictions broadly, GDPR Article 9 (special-category data) may also bear on combinations like party-affiliation × extremism-coding. Legal review before public release of ranked findings is mandatory; the schema's discipline (speech-act labelling, evidence-anchored, no personality attribution) is necessary but not sufficient.
6. **Ground-truth gap on "lies" and "misinformation".** This schema deliberately does not code these; consumers asking "which politician lies most" should be told the question is not text-only-answerable and offered the rhetorical-fallacy density proxy instead.
7. **Validation κ must be re-run periodically.** Even with no classifier upgrades, source distribution drifts (new political actors, new rhetorical styles). Re-validate κ against the gold sample annually; refresh the gold sample every 3–5 years.
8. **Era-scoped markers can produce anachronisms.** `parallel_state` is post-2017, `băsism` is post-2004, `country_for_sale` peaks during privatisation debates, `comunisto_securist` post-1989. Each marker definition carries an optional `era_window`; the validator emits a warning when the LLM tags a marker outside its window. Treat the warning as a near-certain false positive unless the context is *explicit historical comparison* (e.g. a 2024 speech recalling 1990s "country-for-sale" rhetoric).
9. **Securitization is conceptually contested.** The Copenhagen-School rubric is widely cited but its operationalisation across studies is uneven. The schema captures the canonical *speech-act move* (existential framing → referent → extraordinary measures) but consumers should treat the binary `securitization_move_present` as a heuristic, not an arbitral judgment. Validation κ on this axis is expected to be lower than Hawkins or DQI.
10. **DQI-on-Romania has no published baseline.** DQI has been applied to many parliaments but not, to our knowledge, to Romania at corpus scale. The framework rubric transfers cleanly in principle (justification, respect, constructive politics are language-neutral concepts) but Romanian-specific calibration is part of the gold-sample work. Until then, DQI codings carry standard-rubric authority but not Romania-specific empirical anchoring.
11. **Audience-reaction signals depend on stenographer practice.** Romanian stenographers vary in how exhaustively they record `(Aplauze.)`, `(Rumoare.)`, `(intervenții din sală)` — older transcripts are sparser. Per-period normalisation may be needed before comparing audience-disruption indices across decades. Document the convention in `canonical-queries.md` for any audience-signal-based query.
12. **Asymmetry of the layer.** v0.1 coded only pathology. v0.2 adds DQI as the positive deliberative-quality axis but the layer remains lopsided: many marker families code negative discourse, only one (DQI) codes positive. Consumers using the layer for full-spectrum political-science analysis (rather than risk detection) should report this asymmetry explicitly.
13. **Written-vs-delivered conflation.** Written interpellations (`întrebări scrise`) and delivered floor speeches follow different rhetorical norms. The `delivery_mode` field on each coding distinguishes them; queries should filter by `delivery_mode = "delivered"` for floor-rhetoric rankings unless intentionally including written items.

---

This document records the design at version 0.2.0 of the discourse-analysis layer. v0.2 added decision Q12 (audience-reaction signals), promoted DQI to a primary framework, split the v0.1 lumped `rhetorical_acts` bucket into per-phenomenon custom subtrees (`insults`, `fearmongering`, `victimhood`, `fallacies`), introduced new custom buckets (`securitization`, `conspiracy_framing`, `apologia`, `geopolitical_alignment`, `gendered_rhetoric`, `climate_denial`, `pandemic_health`), expanded the `romanian_specific` markers (`parallel_state`, `antonescu_rehabilitation`, `holocaust_relativisation`, `country_for_sale`, `corectitudine_politica`, etc.), extended the rhetorical-moves enum (`prefacing_then_claiming`, `just_asking_questions`, `concern_trolling`, `false_concession`, `ironic_distancing`), added `era_window` scoping on markers, added `delivery_mode` and `skipped_short` per coding, and added the `--min-words` floor on the `analyze` subcommand. Subsequent revisions append new sections in the same `Q-N` style as `extraction-schema.md`'s audit revisions, never rewriting the design tree above.
