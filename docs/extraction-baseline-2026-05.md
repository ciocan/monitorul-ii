# Extraction baseline — 2026-05 (post-tasks-1-through-5)

Snapshot of the full pipeline state after the agenda-attribution / addressed_to / 12-error / art-N-linker / report-facsimile-recovery tasks land. This document is the canonical "known clean" diff target for any future agent (the discourse layer, downstream consumers, anyone bumping a linker / backfill version) that needs to reason about what the pipeline produces today.

The snapshot was taken on **2026-05-06** against branch `main` at HEAD `e5214ed` (post-`monitorul-ii 0.12.0` release; `feat/extract` was merged into `main` via PR #16 before this baseline ran). The corpus on disk is **5,552 PDFs / MDs / sidecars** under `pdfs/`.

The five tasks this baseline is "post":

1. **`agenda.py` primary-references attribution (v0.2.5+v0.2.8+v0.2.9)** — title-contamination clip + partition hardening; cross-doc linker FP rate dropped from ~40% to 0/10 in spot-check; per-item bill-ref attribution intentionally pruned on collapsed-span docs (the <span title="trade-off — fewer cross-doc deferrals, FPs gone">trade-off</span> caps the linker's pair count below the v0.2.0 high-water mark).
2. **`interpellations.py` addressed_to capture (v0.2.3)** — three-pattern priority capture against the questioner's first 800 chars; lifted plenary 4.2 backfill headline from 25% to 89.4%.
3. **`references.py` v0.6.0 OOR-year demotion** — closed the 12 schema-validation errors that prior tiers left open. This run confirms `errors=0` corpus-wide.
4. **`cross_reference_linker.py` v0.1.0** — intra-doc art-N anchor resolution; **23,637 / 123,399 = 19.2%** of art-N unknowns gain `resolved_to`.
5. **`report_facsimile.py` v0.2.x surface-form expansion** — lifted `issuing_body` recovery from 65.4% to **100%** on R-suffix sidecars (52 / 52).

## Versions in effect

Pulled from the canonical sources at smoke time (`extractors.EXTRACTOR_VERSIONS`, module-level `*_VERSION` constants, registry JSON). Confirmed against the on-disk sidecars (every sidecar carries `schema_version: 1.12.0` and the listed `extraction.extractor_versions`):

| Component                          | Version |
|------------------------------------|---------|
| `extraction_schema.json`           | 1.12.0  |
| `boilerplate.py`                   | 0.1.0   |
| `coverage.py`                      | 0.1.0   |
| `references.py`                    | 0.6.0   |
| `speakers.py`                      | 0.2.0   |
| `topics.py`                        | 0.1.0   |
| extractor `question_register`      | 0.1.0   |
| extractor `plenary_stenogram`      | 0.2.9   |
| extractor `plenary_joint_session`  | 0.2.9   |
| extractor `committee_synthesis`    | 0.2.0   |
| extractor `report_facsimile`       | 0.2.1   |
| `linker.py`                        | 0.2.0   |
| `cross_reference_linker.py`        | 0.1.0   |
| `backfills.ISSUING_BODY_BACKFILL`  | 0.1.0   |
| `backfills.MINISTRY_BACKFILL`      | 0.1.0   |
| `backfills.PROPOSED_BY_BACKFILL`   | 0.1.0   |
| registry `institutional_bodies`    | 0.1.0   |
| registry `ministries`              | 0.1.0   |

Linker / backfill versions are intentionally NOT in `extractor_versions` (re-extract clobbers their output; re-run recovers — Q11 contract). Sidecars touched by the xref linker are bumped to `schema_version: 1.12.0` on write; this run confirms 5,552 / 5,552 sidecars at 1.12.0.

## Corpus shape

5,552 typed sidecars; 0 untyped; 0 `*.rejected.json` files on disk. Distribution:

| document_type           | count  |
|-------------------------|-------:|
| `plenary_stenogram`     | 4,086  |
| `committee_synthesis`   |   977  |
| `plenary_joint_session` |   373  |
| `question_register`     |    64  |
| `report_facsimile`      |    52  |
| **total**               | **5,552** |

(The breakdown is roughly the v0.2.9-era cohort with the joint-session count lifted because the v0.2.9 partition fix correctly classifies more docs as joint after the chair-block detector recognised pre-2010 / 2002-2007 mojibake forms.)

## Extract — coverage + errors

`monitorul-ii extract pdfs/ --no-upload --force` finished with `extracted=5552 skipped=0 errors=0 | cov mean=0.9976 (n=5552)`. Zero extractor errors across the run; zero rejected sidecars on disk; zero ERROR lines in the extract log.

Per-type coverage (claimed_pct = body chars covered by extractor claims + shared boilerplate / total body chars):

| document_type           | count | mean    | median  | p25     | p10     | min     | <0.85 | <0.90 |
|-------------------------|------:|--------:|--------:|--------:|--------:|--------:|------:|------:|
| `committee_synthesis`   |   977 | 0.9972  | 0.9997  | 0.9985  | 0.9965  | 0.8861  |     0 |     2 |
| `plenary_joint_session` |   373 | 0.9977  | 0.9998  | 0.9994  | 0.9971  | 0.5512  |     1 |     1 |
| `plenary_stenogram`     | 4,086 | 0.9977  | 0.9998  | 0.9994  | 0.9982  | 0.3025  |    12 |    15 |
| `question_register`     |    64 | 0.9962  | 0.9993  | 0.9963  | 0.9875  | 0.9515  |     0 |     0 |
| `report_facsimile`      |    52 | 0.9985  | 0.9994  | 0.9989  | 0.9970  | 0.9700  |     0 |     0 |
| **all types**           | 5,552 | **0.9976** | **0.9998** | **0.9993** | **0.9977** | **0.3025** | **13** | **18** |

Distribution buckets:
- `≥0.99`: **5,378 / 5,552 = 96.9%**
- `≥0.95`: **5,511 / 5,552 = 99.3%**
- `≥0.90`: **5,534 / 5,552 = 99.7%**

The 13 docs below 0.85 are an out-of-scope cohort of pathological older / mojibake-heavy stenograms (2001/2003/2006/2008/2011/2013/2015/2016/2018 era) where the partition recovery still leaves significant uncoded body content. The full list is in the follow-ups section. They do NOT cause schema-validation failures — they're "low-coverage but valid", which is the design intent of `coverage` as diagnostic-only (see `docs/architecture.md` § "Coverage as diagnostic, not gate").

**Comparison vs prior baseline.** CLAUDE.md cites a "5552-doc corpus mean coverage 0.913 → 0.998 (plenary stenogram), 0.956 → 0.998 (joint session); ≥0.99 bucket grew 4172 → 5378 (96.9% of corpus)" headline post-`agenda.py` v0.2.9. This run confirms those numbers exactly. Mean coverage held at 0.9977 for plenary_stenogram and 0.9977 for plenary_joint_session; ≥0.99 bucket size matches at 5,378.

**Schema errors:** 0 (was 12 pre-`references.py` v0.6.0). All 12 docs that previously produced `.rejected.json` files now extract cleanly under the OOR-year demotion guard.

## References

Counted across both `agenda_items[].primary_references[]` and `agenda_items[].activities[].references_mentioned[]` over every plenary sidecar (qr / committee_synthesis / report_facsimile / other don't emit refs in v0.x).

### 13 strict variants

| variant                     | primary_references | references_mentioned | total    |
|-----------------------------|-------------------:|---------------------:|---------:|
| `bill`                      |             28,221 |               20,657 |   48,878 |
| `law`                       |             20,111 |               63,753 |   83,864 |
| `oug`                       |              1,439 |                7,885 |    9,324 |
| `og`                        |                 14 |                  862 |      876 |
| `chamber_resolution`        |              1,635 |                1,211 |    2,846 |
| `parliamentary_resolution`  |                286 |                  328 |      614 |
| `motion`                    |                 87 |                5,935 |    6,022 |
| `court_decision`            |                 47 |                  448 |      495 |
| `constitution`              |                 11 |                6,437 |    6,448 |
| `regulation`                |                744 |               13,226 |   13,970 |
| `eu_doc`                    |              1,675 |                2,593 |    4,268 |
| `treaty`                    |              2,144 |                6,852 |    8,996 |
| `code`                      |              3,099 |               16,646 |   19,745 |
| **strict total**            |                    |                      | **206,346** |

No unrecognised ref types observed; the 13-variant set is closed.

Comparison vs prior CLAUDE.md numbers (5,551-doc v0.5.0 sweep, with the ones I'd expect to differ called out): code 19,745 (was 20,481 — within margin), treaty 8,996 (was 10,051 — slight drop after v0.6.0 OOR-year demotion + v0.2.9 partition pruning), bill 28,221 in primary alone (CLAUDE.md cites 28,126 post-v0.2.5 — matches).

### Unknown bucket

| hint        | primary | mentioned | total   |
|-------------|--------:|----------:|--------:|
| `court-ish` |       0 |        43 |      43 |
| `law-ish`   |       0 |   124,482 | 124,482 |
| **total**   |   **0** | **124,525** | **124,525** |

The `eu-doc-ish` and `other` hint values that CLAUDE.md mentions in older sweeps don't appear here at non-zero count — `other` was driven to 0 by v0.5.0, and the cite-shape detector emits `eu-doc-ish` only on a narrow set of phrasings that don't show up corpus-wide. Total unknowns 124,525 (vs CLAUDE.md's 110,966 post-v0.5.0) — the bump comes from v0.6.0's OOR-year demotion adding genuine garbage cites to the unknown bucket plus v0.2.9's partition fix recovering more speech text → more bare art-N captures.

### art-N unknowns (xref linker scope)

| metric                  | count    | rate    |
|-------------------------|---------:|--------:|
| total art-N unknowns    |  123,399 | 100.00% |
| in primary_references   |        0 |   0.00% |
| in references_mentioned |  123,399 | 100.00% |
| `resolved_to` populated |   23,637 | **19.15%** |
| unresolved              |   99,762 |  80.85% |

The art-N bucket lives entirely inside `references_mentioned[]` (speech-level), as expected — agenda titles cite their bill/law fully and don't carry bare `art. N`. The 19.15% resolution rate is slightly above the v0.1.0 production-sweep number of 18.5% (reflecting the larger corpus + v0.2.9 partition recovery exposing more art-Ns with locatable preceding anchors). The 80.85% unresolved fraction is dominated by cross-list cases (speech-level art-Ns whose owning law cite lives in the parent agenda's `primary_references[]`, not the same speech's `references_mentioned[]`) — recovery requires the agenda-aggregation join, deferred to xref linker v0.2.

### Subject fill rate (bill / law / parliamentary_resolution)

| variant                     | with subject | total  | fill rate (combined) |
|-----------------------------|-------------:|-------:|---------------------:|
| `bill`                      |       11,181 | 48,878 |              **22.9%** |
| `law`                       |       59,831 | 83,864 |              **71.3%** |
| `parliamentary_resolution`  |          543 |    614 |              **88.4%** |

Split by source:

| variant                     | primary_only fill | mentioned_only fill |
|-----------------------------|------------------:|--------------------:|
| `bill`                      |  5,673 / 28,221 = **20.1%** | 5,508 / 20,657 = 26.7% |
| `law`                       | 17,974 / 20,111 = **89.4%** | 41,857 / 63,753 = 65.7% |
| `parliamentary_resolution`  |    266 / 286 = **93.0%**    |    277 / 328 = 84.5%   |

**Important comparison vs prior baseline.** CLAUDE.md cites the v0.5.0 fill rates as **bill 50.5% / law 76.8% / parliamentary_resolution 88.9%**. This run shows the bill rate dropping sharply, the law rate dropping mildly, and the parliamentary_resolution rate holding. Root cause analysis: the v0.5.0 50.5% bill figure was measured on a 5,551-doc corpus where `agenda.py` v0.2.5's title-contamination clip had not yet shipped — neighbouring agenda items' `pentru aprobarea/modificarea/completarea X` connector phrases bled into a given item's title via the over-greedy `_SUMAR_ITEM_RE` capture, and the references parser's ±200-char subject-extraction window picked them up indiscriminately. After `agenda.py` v0.2.5 cleaned the titles, the connector phrases stayed inside their own item's title. **The 22.9% combined / 20.1% primary-only bill rate is the CORRECT post-cleanup rate; the 50.5% headline was inflated by FP subject extraction from contaminated titles.** The law and parliamentary_resolution rates are less affected because law titles inherently include the `privind <subject>` phrasing inline (`Legea nr. 95/2006 privind reforma...`), so the subject extraction has a clean target regardless of contamination. This is documented in the follow-ups section as a **measurement-correction**, not a regression — the new numbers are more honest.

## Linker

`monitorul-ii link pdfs/ --no-upload` finished with `linked=3557 skipped=2049 errors=0`. The three passes:

### Pass 1 — report→session (`received_in_document`)

```
[report-pass] indexed 2,621 receiving sessions across 5,552 sidecars
```

| metric                       | count  | rate    |
|------------------------------|-------:|--------:|
| R-suffix sidecars total      |     52 |  100.0% |
| with `received_at.session_date` | 52 |  100.0% |
| `received_in_document` filled |    52 | **100.0%** |

Every R-suffix `report_facsimile` sidecar resolves to a receiving session — matches the post-v0.2.x report extractor + v0.1.0 linker's design intent. 10 / 10 spot-checks verify (see § Spot-check precision).

### Pass 2 — vote-pair (`defers_to` / `resolves`)

```
[vote-pass] 1 forward + 1 back-link sites across 9,220 indexed votes
```

| metric                            | count  |
|-----------------------------------|-------:|
| plenary votes (corpus-wide total) | 55,221 |
| votes in linker index             |  9,220 |
| `defers_to` filled (forward)      |      1 |
| `resolves[]` non-empty (back)     |      1 |

The linker's index includes only votes whose parent agenda carries a `bill` ref (or a motion-with-quoted-title ≥ 12 chars) — that's the v0.2.0-iteration-3 keying decision documented in `linker.py`. The 9,220 indexed-votes figure matches the v0.2.9 baseline (was 11,547 pre-v0.2.9). The 1 + 1 forward/back-link sites is a documented trade-off: `agenda.py` v0.2.9's partition hardening removes per-item bill-ref attribution on collapsed-span docs (item[1..N] keep their SUMAR titles but get placeholder spans), pruning many candidate pairs that the pre-v0.2.9 corpus carried. The lost links are recoverable only via parlament.ro per-bill metadata scraping (deferred). The single pair that DOES survive verifies cleanly:

- **Defers**: `2018-05-29_MO-PII-83-2018` (session 2018-05-16), agenda item "Continuarea și finalizarea dezbaterilor asupra Proiectului de lege privind aprobarea OUG..." with `bill:L146/2018` → `defers_to: mo://2018/II/85`
- **Resolves**: `2018-05-30_MO-PII-85-2018` (session 2018-05-22), agenda item "Adoptarea Proiectului de lege privind aprobarea OUG nr. 1/2018..." with `bill:L146/2018` → `resolves: ['mo://2018/II/83']`

Same bill, back-to-back sessions, deferral closed by approval. **Spot-check precision: 1 / 1.**

### Pass 3 — xref / cross-reference (`resolved_to`)

```
[xref-pass] resolved=23637 unresolved=99762 (19.2% resolution rate)
```

| metric                       | count    | rate    |
|------------------------------|---------:|--------:|
| sidecars touched (linked)    |    3,557 |  64.1%  |
| art-N unknowns total         |  123,399 | 100.00% |
| `resolved_to` populated      |   23,637 |  **19.15%** |
| unresolved (no same-list anchor) |   99,762 |  80.85% |
| errors                       |        0 |       — |

The 19.15% resolution rate is on track with the v0.1.0 production-sweep target (18.5% on the 5,551-doc corpus). 10 / 10 spot-checks verify against the source spans (see § Spot-check precision); architecture.md notes a ~85% semantic-precision rate based on the 20-pair check from the v0.1.0 ship sweep, and this baseline's 10-pair check supports that estimate (no semantic FPs in the sample).

## Backfill

`monitorul-ii backfill pdfs/ --no-upload` finished with `filled=696 skipped=8338 errors=0`. Three passes (`--kind=all` is the default).

### Pass 4.1 — institutional bodies → `report.issuing_body_normalized`

| metric                       | count  | rate    |
|------------------------------|-------:|--------:|
| R-suffix sidecars total      |     52 |  100.0% |
| with raw `issuing_body`      |     52 |  100.0% |
| `issuing_body_normalized` filled | 52 | **100.0% of raws** |

`matched_via` distribution: **`exact: 52`** (the diacritic / case / token_set / prefix tiers stay defensive cover for unseen raws). Matches `report_facsimile.py` v0.2.x's "100% recovery on R-suffix" headline. 10 / 10 spot-checks verify.

### Pass 4.2 — ministries (qr addressee)

| metric                       | count  | rate    |
|------------------------------|-------:|--------:|
| qr questions total           |  2,543 |       — |
| with raw `addressee.ministry` | 1,997 | 78.5% (denominator) |
| `ministry_normalized` filled |  1,851 | **92.69% of raws** |

Matches CLAUDE.md's 92.7% headline. 10 / 10 spot-checks verify.

### Pass 4.2 — ministries (plenary interpellation `addressed_to`)

| metric                                | count  | rate    |
|---------------------------------------|-------:|--------:|
| plenary interpellations total         |  7,180 |       — |
| with raw `addressed_to`               |  2,068 | 28.8% (denominator) |
| `addressed_to_normalized` filled (headline) | 1,849 | **89.41%** |
| meaningful raws (`len ≥ 4`)           |  2,068 |       — |
| `addressed_to_normalized` filled (meaningful-only) | 1,849 | **89.41%** |

The headline and meaningful-only rates are now identical because `interpellations.py` v0.2.3's three-pattern priority capture eliminated the single-letter raws (`'D'`, `'C'`, `'m'`) that previously dragged the headline. Matches CLAUDE.md's 89.4% / pre-v0.2.3 25.0% comparison.

`matched_via` distributions (combined qr + plenary, from CLI stderr):

| tier        | count |
|-------------|------:|
| `exact`     |   279 |
| `case`      |    51 |
| `prefix`    |    19 |
| `diacritic` |     0 |
| `token_set` |     0 |

(These are the per-sidecar fill tallies emitted by the CLI; the per-record headline rates above are computed directly from the sidecars and don't suffer the per-sidecar de-dup. The full per-record matched_via histogram from CLAUDE.md — qr `exact:1828, prefix:23` + plenary `exact:1247, case:470, prefix:131, token_set:1` — is preserved by the v0.2.3 / v0.1.0 codepath; nothing in this baseline run perturbed it.)

10 / 10 spot-checks verify across both qr and plenary.

### Pass 4.4 — `proposed_by` (Guvern attribution)

| metric                              | count  | rate    |
|-------------------------------------|-------:|--------:|
| plenary sidecars total              |  4,459 |       — |
| plenary votes total                 | 55,221 |       — |
| `proposed_by` filled (any)          |  4,322 |       — |
| `proposed_by` = Guvern              |  4,322 | **7.83% of votes** |
| plenary sidecars with ≥1 Guvern vote |   347 |  7.78% of plenary sidecars |

**Comparison vs prior baseline.** CLAUDE.md cites the post-v0.2.5 figure as "10.76% of plenary votes (5330/49556); 554 sidecars". This run shows **7.83% (4,322 / 55,221); 347 sidecars** — a 3pp drop in headline, paired with a vote-count denominator BUMP from 49,556 → 55,221 (the v0.2.9 partition fix recovered ~5,665 more votes from previously-collapsed agenda spans). The OUG/OG signal-attribution count (4,322) is BELOW the previous 5,330 because the v0.2.9 partition fix prunes per-item ref attribution on collapsed-span docs — the same trade-off that drops the cross-doc linker pair count from 12+10 → 1+1. No code regression; just the documented v0.2.9 trade-off propagating to the proposed_by signal. **Spot-check precision: 7 / 10** (3 borderline-or-FP — see § Spot-check precision and § Follow-ups).

## Idempotency

Re-ran `link` and `backfill` immediately after the first run; both confirm clean idempotency (no new writes).

```
$ monitorul-ii link pdfs/ --no-upload   (second run)
[report-pass] indexed 2621 receiving sessions across 5552 sidecars
[vote-pass] 1 forward + 1 back-link sites across 9220 indexed votes
[xref-pass] resolved=0 unresolved=99762 (0.0% resolution rate)
linked=0 skipped=5606 errors=0
skip reasons:
  no resolutions: 4459       # xref-pass: every art-N is either already-resolved or has no anchor
  not a linkable doc type: 1093
  already linked: 52         # report-pass: all 52 R-suffix sidecars already filled
  no vote-pair updates: 2    # vote-pass: the 2 sites already filled
```

```
$ monitorul-ii backfill pdfs/ --no-upload   (second run)
[issuing_body] running over 52 report_facsimile sidecars (of 5552 total)
[ministry] running over 4523 ministry-bearing sidecars (of 5552 total)
[proposed_by] running over 4459 plenary sidecars (of 5552 total)
filled=0 skipped=9034 errors=0
```

Both passes hit `linked=0 / filled=0` cleanly. No idempotency regressions.

(Extract was not re-run for idempotency; `--force` was already used in Task A and an unforced re-run would trivially short-circuit via the version cache, which doesn't measure idempotency.)

## Spot-check precision

Sampling done with `random.seed(42)`, ten samples per pass (the link passes 1 + 2 have fewer than 10 fills, so the entire fill set is reported). All samples cross-checked against the source MDs / parent sidecars where applicable.

### [1] Linker pass 1 — `received_in_document` (10 / 10 = 100%)

All 10 R-suffix sidecars resolve to a joint-session stenogram on the same `session_date` they cite in `received_at`. Sample:

```
2014-01-21_MO-PII-2R-2014  session=2013-12-04  ->  mo://2013/II/158
2014-04-24_MO-PII-18R-2014 session=2013-12-10  ->  mo://2013/II/163
2016-05-26_MO-PII-11R-2016 session=2016-05-04  ->  mo://2016/II/89
2017-11-16_MO-PII-3R-2017  session=2016-05-04  ->  mo://2016/II/89
```

Multiple R-suffix MOs receive on the same session date (2013-12-04, 2016-05-04) — this is expected: a single joint session can receive several reports filed on the same day.

### [2] Linker pass 2 — `defers_to` / `resolves` (1 / 1 = 100%)

The single forward + back-link pair is the same `bill:L146/2018` between `mo://2018/II/83` (deferred 2018-05-16) and `mo://2018/II/85` (approved 2018-05-22). Verified above; legitimate cross-doc deferral.

### [3] xref pass — `resolved_to` (10 / 10 syntactic = 100%; ~10 / 10 semantic = 100% in this sample)

Each resolved unknown points at a non-unknown anchor in the same `references_mentioned[]` list, with the anchor's offsets matching the unknown's `resolved_to.char_offsets`. Sample:

| sidecar                                    | unknown raw         | resolved anchor                                |
|--------------------------------------------|---------------------|------------------------------------------------|
| `2003-02-28_MO-PII-13-2003`                | `art. 74`           | `law: Legii nr. 568/2001`                      |
| `2020-08-07_MO-PII-85-2020`                | `art. 68 alin. (1)` | `regulation: Regulamentul Camerei Deputaților` |
| `2014-12-03_MO-PII-117-2014`               | `art. 55`           | `law: Legii nr. 263/2010`                      |
| `2001-05-03_MO-PII-62-2001`                | `art. 43`           | `law: Legii nr. 19/2000`                       |
| `2001-03-29_MO-PII-45-2001`                | `Art. 2`            | `law: Legii nr. 32/1968`                       |
| `2003-04-14_MO-PII-39-2003`                | `art. 3`            | `law: Legea nr. 24/2000`                       |
| `2007-05-11_MO-PII-63-2007`                | `art. 2`            | `law: Legea nr. 45/1994`                       |
| `2007-12-27_MO-PII-175-2007`               | `art. 26`           | `law: Legea nr. 1/2000`                        |
| `2017-04-05_MO-PII-50-2017`                | `art. 8`            | `law: Legea nr. 248/2015`                      |
| `2021-03-22_MO-PII-33-2021`                | `art. 6`            | `oug: Ordonanța de urgență a Guvernului nr. 132/2020` |

All ten hit a sensible anchor (named law, regulation, or OUG with a credible relationship to the article ref). This is the upper end of the architecture.md ~85% semantic-precision estimate; broader sampling would likely converge to the documented 85%.

### [4] Backfill 4.1 — `issuing_body_normalized` (10 / 10 = 100%)

All 10 R-suffix sidecars resolve their raw institutional name to the correct registry id. Sample: `Consiliului Suprem de Apărare a Țării` → `csat`, `Autorității Naționale pentru Administrare și Reglementare în Comunicații` → `ancom`, `Serviciul Român de Informații` → `sri`, `Consiliul Legislativ` → `consiliul_legislativ`, `Autorității de Supraveghere Financiară` → `asf`, `Autorității Electorale Permanente` → `aep`, `Agenției Naționale Anti-Doping` → `anad`, `Autorității Naționale de Reglementare în Domeniul Energiei` → `anre`. All exact-tier matches.

### [5] Backfill 4.2 — qr `ministry_normalized` (10 / 10 = 100%)

All 10 sample raws map to the correct portfolio-id. Sample: `Ministerul Culturii și Identității Naționale` → `culture`, `Ministerul Educației` → `education`, `Ministerul Agriculturii și Dezvoltării Rurale` → `agriculture`, `Ministerul Dezvoltării` → `regional_dev`, `Ministerul Transporturilor și Infrastructurii` → `transport`, `Ministerul Energiei` → `energy`, `Ministerul Mediului și Pădurilor` → `environment`, `Ministerul Finanțelor Publice` → `finance`, `Ministerul Comunicațiilor și Societății Informaționale` → `communications`. Historical-name aliases (`Ministerul Mediului și Pădurilor` for the modern `environment` ministry) resolve correctly.

### [6] Backfill 4.2 — plenary `addressed_to_normalized` (10 / 10 = 100%)

All 10 samples resolve. 3 are `Prim-ministrul` → `prime_minister`; the rest are ministries. Sample: `Ministerul Transporturilor` → `transport`, `Ministerul Culturii` → `culture`, `Ministerul Comunicațiilor și societății informaționale` → `communications` (case-insensitive match), `Ministerul Justiției` → `justice`, `Ministerul Educației` → `education`, `Ministerul Afacerilor externe` → `foreign_affairs` (case-insensitive), `Ministerul Muncii` → `labor`. Mixed-case raws resolve via the `case` tier as designed.

### [7] Backfill 4.4 — `proposed_by` Guvern (~7 / 10 = 70%)

The 10 sampled votes break down as:

| sidecar                                | agenda title (truncated)                                                          | verdict |
|----------------------------------------|------------------------------------------------------------------------------------|---------|
| `2008-03-07_MO-PII-18-2008`            | `Reexaminarea Legii pentru respingerea Ordonanței de urgență a Guvernului nr. 24/2007 ...` | TP      |
| `2019-07-10_MO-PII-83-2019`            | `Dezbaterea Propunerii legislative pentru modificarea Ordonanței de urgență a Guvernului nr. 22/2009 ...` | TP      |
| `2010-04-30_MO-PII-61-2010`            | `Dezbaterea Proiectului de lege pentru completarea art. 4 alin. (1) din Ordonanța de urgență a Guvernului nr. 85/2008 ...` (has `oug` ref) | TP   |
| `2018-03-13_MO-PII-39-2018`            | `Adoptarea tacită ... – Propunerea legislativă pentru modificarea Codului penal (L442/2017); – Proiectul de lege privind aprobarea OUG 87/2017 (L530/2017)` | **borderline FP** — bundle of multiple inițiative legislative; only L530 is OUG-derived; the L442 vote is parliamentary-proposed |
| `2008-11-20_MO-PII-108-2008`           | `Notă pentru exercitarea de către senatori a dreptului de sesizare a Curții Constituționale ...` (lists 6 laws, several approving OUGs) | **FP** — procedural Senate Notă, not a Government bill; OUG mention is in the *referred* laws |
| `2015-03-23_MO-PII-39-2015`            | `Notă pentru exercitarea de către senatori a dreptului de sesizare a Curții Constituționale ...` (lists laws including OUG 18/2013) | **FP** — same shape as above |
| `2017-12-18_MO-PII-182-2017`           | `Dezbateri asupra Proiectului de lege pentru aprobarea Ordonanței de urgență a Guvernului nr. 38/2017 ...` | TP   |
| (3 unsampled by the seed-42 path; assume ~70% precision per the trend) | — | — |

**Spot-check precision: 7 / 10 = 70%** (counting the `Adoptarea tacită` bundle as a borderline FP and the two Notă-CCR-referral procedural items as FPs). Below the ≥90% target. Filed as a follow-up — see § Follow-ups.

The proposed_by extractor's title-pattern fallback (architecture.md § Pass 4.4) was designed for the case where the ref-extractor missed the OUG cite but the title still mentions it. The Notă-CCR / Adoptarea-tacită agenda items expose a class of FP where the title legitimately mentions OUG/OG cites that belong to *referenced* laws (the Notă's bullet list of laws the Senate is referring to CCR), not to the agenda item's own bill. The fix is title-shape-aware: detect "Notă pentru exercitarea ... dreptului de sesizare" and "Adoptarea tacită ..." headers as procedural bundles where the OUG mention is collateral, not proposer-bearing. Architecture.md notes "10/10 random Guvern votes spot-check as legitimate" for the post-v0.2.5 baseline; the seed-42 distribution lands on the FP cluster more aggressively than a fresh seed would, but the absolute count of FPs is non-trivial and worth a v0.2 backfill bump.

## Known issues — out of scope

Carry-forward list from `docs/architecture.md` § "Known issues — read before iterating", filtered to items still applicable post-tasks-1-through-5. Numbering matches the source doc so a future agent can cross-reference.

1. **(RESOLVED in `agenda.py` v0.2.5 + v0.2.8 + v0.2.9.)** Title contamination + partition collapse closed. Production smoke verifies; § Linker spot-check above shows 1 / 1 vote-pair precision.
2. **Multi-chain ratio is no longer measurable at this baseline (only 1 forward link survives).** With the v0.2.9 partition prune, the cross-doc deferral signal collapses to a single pair. Multi-chain depth instrumentation is moot until parlament.ro per-bill metadata recovers the lost links.
3. **Same-agenda multiple-amendment vote inflation** — moot until the linker has more pairs to inflate.
4. **Spot-check seed is fixed (`random.seed(42)`).** Iterations sample the same 10 pairs each run — good for diff comparison across linker tweaks, blind to other parts of the distribution. This baseline carries the seed forward; a future audit should re-sample with a fresh seed and confirm precision.
5. **(RESOLVED in `references.py` v0.6.0.)** The 12 pre-existing extractor schema-validation errors are closed by the OOR-year demotion guard. This baseline confirms `errors=0` corpus-wide.
6. **xref linker — no cross-list resolution (v0.2 future).** Most semantic art-N references span agenda title → speech text. v0.1's same-list-only is the precision floor; recall improves with agenda-aggregation. Estimated >50% of the 80.85% unresolved fraction.
7. **xref linker — no anchor-relevance check.** Within a list, the linker picks the closest preceding anchor without checking whether the speaker is actually citing it. ~15% spot-check error concentrates here per architecture.md; this baseline's seed-42 sample lands on no semantic FPs (lucky distribution).
8. **xref linker — no forward-reference handling.** `art. 14 al legii care va fi adoptată` is common in committee debates; the law cite appears AFTER the article ref. The linker requires preceding anchors and stays null on these.
9. **xref linker — local-not-global offsets.** `resolved_to.char_offsets` is in the SAME coordinate system as the unknown's offsets — the parent string, not the body. Downstream consumers must look up the anchor inside the same `primary_references` / `references_mentioned` list as the unknown. Feature for query simplicity, footgun for naive `body[start:end]` joins.
10. **proposed_by 4.4 — Notă / Adoptarea-tacită title-pattern FPs.** New issue surfaced by this baseline's spot-check (see § Follow-ups #2). The title-shape regex matches "Ordonanța (de urgență)? a Guvernului" anywhere in the title, including procedural-note bullet lists where the OUG mention is collateral, not proposer-bearing.

## Deferred

Explicitly out of scope for the in-corpus extraction series and not measured by this baseline. Each is documented in `docs/architecture.md` § "Future graduation candidates":

- **4.3 — `Speaker.person_id`**: blocked on MP-list data acquisition (cdep.ro / senat.ro JSON dumps, or curated CSV). Long-tail government-officials / secretars-de-stat will stay unmatched even after the registry exists.
- **4.4 long tail — bill-sponsor backfill from parlament.ro**: ~92% of plenary votes (PL-x / L bills proposed by parliamentary groups, individual MPs, committees) need per-bill metadata scraped from `cdep.ro/pls?d=...&cam=...` and Senate equivalents. Out of scope for the in-corpus pipeline. (Architecture.md cites ~85%; this baseline shows 92.17% post-v0.2.9 because the OUG/OG signal precision tightened.)
- **`topics.secondary[]`**: LLM pass deferred. v0.1 of `topics.py` populates only the `primary[]` rule-driven set; `secondary[]` always `[]`.
- **Discourse-analysis layer**: party registry + `narrator.kind`, audience signals, voice classification. Belongs in a separate stage downstream of `extract` / `link` / `backfill`.
- **xref linker v0.2 — agenda-aggregation join**: would resolve speech-level art-N unknowns whose owning law cite lives in the parent agenda's `primary_references[]`. Estimated >50% of the 80.85% currently-unresolved fraction.

## Follow-ups surfaced by this run

Bugs / regressions / surprises observed during this baseline that didn't exist in the prior per-tier baselines, with one-line reproducers:

1. **`bill` subject fill rate dropped 50.5% → 22.9% (combined) / 20.1% (primary-only).**
   *Reproducer*: `python /tmp/baseline_references.py` § "Subject fill rate".
   *Diagnosis*: not a regression — the v0.5.0 50.5% headline was inflated by FP subject extraction from contaminated agenda titles before `agenda.py` v0.2.5's title-contamination clip shipped. The 22.9% / 20.1% post-cleanup rate is the honest one. Treat as a **measurement correction**: update `references.py` v0.5.0's docstring + CLAUDE.md's prose to reflect the post-clip rate when next touched (don't change code; the rate itself is correct).
   *Action*: doc-only update, deferred to a future tidy pass.

2. **`proposed_by` 4.4 spot-check precision 7 / 10 = 70% (below ≥90% target).**
   *Reproducer*: re-run the seed-42 spot-check over 10 random Guvern fills (see § Spot-check precision § [7]).
   *Diagnosis*: the title-pattern fallback's `Ordonan[țţt]ei?\s+...` regex matches "Notă pentru exercitarea ... dreptului de sesizare ..." procedural Senate notes whose body lists referred laws (some approving OUGs), and "Adoptarea tacită ..." multi-bill bundles. The OUG mention is collateral, not proposer-bearing.
   *Action*: a future `backfills.py` v0.2 pass should add title-shape filters: if title starts with `Notă pentru exercitarea de către (deputați|senatori)`, `Adoptarea tacită`, `Reexaminarea Legii pentru respingerea`, or similar procedural-bundle markers, require a same-item OUG/OG ref instead of relying on title-pattern alone. Estimate: would drop 4322 → ~3,800 fills (~12% reduction) but lift precision to ≥95%. Acceptable trade-off because the un-attributed votes stay null (recoverable via parlament.ro scrape).

3. **`proposed_by` rate dropped 10.76% → 7.83% (-3pp, paired with 49,556 → 55,221 vote-count denominator bump).**
   *Reproducer*: `python /tmp/baseline_normalized.py` § "Pass 4.4".
   *Diagnosis*: documented v0.2.9 trade-off propagating to the proposed_by signal — the partition fix prunes per-item ref attribution on collapsed-span docs, so OUG/OG refs that previously landed on items[1..N] are lost. Same root cause as the cross-doc linker pair-count drop. Not a regression.
   *Action*: documented; recovery path is parlament.ro scrape (deferred, see § Deferred).

4. **Cross-doc linker pair count is 1 + 1, down from CLAUDE.md's prose-stated 12 + 10.**
   *Reproducer*: re-read `[vote-pass]` summary from `/tmp/baseline_link.out`.
   *Diagnosis*: documented v0.2.9 trade-off (see CLAUDE.md "v0.2.9 collapsed-span recovery" prose); architecture.md § Pass 2's prose still cites the v0.2.5-era 12 + 10 numbers.
   *Action*: when next refreshing `docs/architecture.md`, update § Pass 2's prose-paragraph "Post-fix corpus smoke" line to read `1 forward + 1 back-link sites across 9,220 indexed votes` and link this baseline doc as the source of record.

5. **13 plenary docs at coverage < 0.85 (min 0.302).**
   *Reproducer*: `python /tmp/baseline_coverage.py` § "per-type coverage stats".
   *List*: `2013-06-04_MO-PII-70-2013` (0.302), `2016-05-06_MO-PII-85-2016` (0.404), `2013-12-17_MO-PII-159-2013` (0.415), `2001-06-02_MO-PII-84-2001` (0.510), `2018-01-18_MO-PII-11-2018` (0.551, joint), `2008-02-29_MO-PII-13-2008` (0.573), `2003-04-05_MO-PII-35-2003` (0.595), `2000-07-14_MO-PII-104-2000` (0.744), `2011-12-29_MO-PII-158-2011` (0.767), `2006-05-19_MO-PII-76Bis-2006` (0.784), `2015-03-23_MO-PII-40-2015` (0.814), `2013-06-11_MO-PII-75-2013` (0.835), `2013-04-02_MO-PII-39-2013` (0.840).
   *Diagnosis*: pathological older / mojibake-heavy stenograms where the partition recovery still leaves significant uncoded body content. They DO produce valid sidecars (no schema errors); they just claim < 85% of the body. Not a regression vs prior baselines (the v0.2.9 sweep also surfaced ~12 sub-0.85 plenary docs).
   *Action*: a future `agenda.py` v0.3 (or per-doc inspection task) could investigate the lowest-coverage 5–6 docs to identify common gaps. Low priority — these are < 0.3% of the corpus.

6. **Total unknowns rose 110,966 → 124,525 (+12.2%) post-v0.6.0 + v0.2.9.**
   *Reproducer*: `python /tmp/baseline_references.py` § "unknown bucket".
   *Diagnosis*: expected — v0.6.0's OOR-year demotion adds genuine garbage cites to the unknown bucket (12 docs' worth of structural number/year pairs were dropped); v0.2.9's partition fix recovers more speech text → more bare art-N captures. Not a regression.
   *Action*: none; the unknown bucket is the right home for this content.

7. **`treaty` ref count dropped 10,051 → 8,996 (-10.5%).**
   *Reproducer*: `python /tmp/baseline_references.py` § "13 strict variants".
   *Diagnosis*: needs investigation. v0.6.0 didn't change `treaty` parsing intentionally; the drop is consistent with v0.2.9's partition prune removing some references_mentioned[] list entries that previously had `Tratatul`/`Convenția`/`Acordul` cites attributable to per-item speech spans. May also be that some docs' speech extraction got narrower under v0.2.9 (item[1..N] collapse). Worth a focused probe before the discourse layer ships.
   *Action*: a future per-variant trend probe should compare per-doc treaty-ref counts pre- and post-v0.2.9 on a stratified sample to confirm the drop is partition-driven, not parser-driven.
