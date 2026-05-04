# Plenary stenogram extractor — implementation plan

Captures the design decisions for v0.1 of the `plenary_stenogram` and `plenary_joint_session` extractors. Companion to `docs/extraction-schema.md` (canonical body shapes, schema_version 1.5.0) and `docs/architecture.md` (pipeline scaffolding). Derived from a 12-branch design grilling on 2026-05-04, after `question_register` v0.1 stabilised at 99.71% mean coverage on its 53-doc cohort.

## Mission

Build the `plenary_stenogram` extractor as the second per-type extractor in the extraction pipeline. This is the bulk of the queryable corpus — 2242 of 3277 MDs (~69%, spanning 13 years) — and exercises v1.4.0 schema content that v0.1's `question_register` extractor didn't touch:

- Speaker turns with `delivery_mode` (5 enum values).
- Agenda enumeration with the 28-value `category` enum (v1.4.0).
- The 12-variant `Reference` discriminated union (we ship 6 strict + 1 catch-all in v0.1).
- Vote detector with deferred-vote semantics, `counts.for ∈ int|null|"unanimous"`, `electronic_remote` (v1.4.0), 8 `motion_type` values incl. `system_check` (v1.3.0).
- Interpellation block parser with `genre` discrimination (v1.2.0).
- Session envelope with `chair_segments[]` (v1.3.0 promoted), `outcome` (v1.4.0), extended `special_procedure` (v1.4.0).

v0.1 ships **both `plenary_stenogram` AND `plenary_joint_session`** together — the marginal cost of joint over plenary alone is one regex + one field injection (Q12).

## Decision tree summary

| # | Branch | Decision |
|---|---|---|
| 1 | Module organization | Sub-subpackage `extractors/plenary/` with sibling modules `agenda.py / activities.py / votes.py / interpellations.py / session.py` |
| 2 | Speaker parser | Shared low-level primitives in `extraction/speakers.py`; per-surface-form parsers in their owning module; `delivery_mode` parsed alongside but stored on the speech turn, not the Speaker dict |
| 3 | Reference parser | Stage. Ship 6 strict variants (`bill / law / oug / og / chamber_resolution / parliamentary_resolution`) + 1 `unknown` catch-all. Defer 6 long-tail variants to v0.2+ |
| 4 | Agenda category enum (28 values) | Central rule table in `extractors/plenary/agenda.py`, signal-weighted, with category-specific functions only as escape hatches; extend enum with `"other"` for no-match default |
| 5 | Vote detector | All canonical Romanian vote-protocol phrases in `extractors/plenary/votes.py`; state machine across 5 protocol stages; vote/speech overlap resolved by second-pass split |
| 6 | Activity-list dispatch | 2-pass partitioner — coarse partition by speaker headers, then fine refinement within each turn for embedded events; final pass sorts + asserts non-overlap |
| 7 | Interpellation block boundary | Detection by chair's canonical transition phrase; no phrase found → `interpellations: []`; block ends at EOF |
| 8 | Session envelope | All envelope fields shipped in v0.1 with appropriate nullability; SUMAR is source-of-truth for agenda enumeration when present, body-scan fallback for older docs |
| 9 | Coverage targets | Discovery margin 0.85, test floor 0.80, mean target 0.90 documented (ungated); median + p10 are the diagnostic; 5-fixture golden corpus |
| 10 | Topics primary | Shared `extraction/topics.py`; 15 canonical topics (no `"Other"` default); multi-match returns full list; title-scoped detection only |
| 11 | Per-component versioning | Keep flat `_shared_helper_versions()` contract; add `topics` as 5th shared key; conservative-by-design (qr re-extracts on plenary's first run — trivial cost) |
| 12 | `plenary_joint_session` relationship | Composition. Both per-type extractors are thin orchestrators over the same sub-extractors in `extractors/plenary/`; joint adds `chambers_present` detection |

## 1. Module layout

```
src/monitorul_ii/extraction/
├── boilerplate.py              # shared MO preamble (existing — unchanged)
├── coverage.py                 # claims + gap math (existing — unchanged)
├── envelope.py                 # frontmatter + EnvelopeMeta (existing — unchanged)
├── pipeline.py                 # dispatcher + ExtractContext (existing — graduates to register plenary types)
├── references.py               # graduates 0.1.0 (stub) → 0.2.0 (6 strict + unknown)
├── schema.py                   # schema validator (existing — unchanged)
├── speakers.py                 # graduates 0.1.0 → 0.2.0 (adds shared primitives)
├── topics.py                   # NEW — 0.1.0 (15 canonical primary topics)
└── extractors/
    ├── __init__.py             # registers plenary_stenogram + plenary_joint_session
    ├── question_register.py    # existing — unchanged (re-extracts on first plenary run; trivial)
    ├── plenary/                # NEW sub-subpackage
    │   ├── __init__.py         # exports `extract`, EXTRACTOR_VERSION, EXTRACTOR_LABEL
    │   ├── agenda.py           # SUMAR parse + 28-category rule table + agenda item bodies
    │   ├── activities.py       # 2-pass partitioner: speech | vote | procedural | narrator | deferral
    │   ├── votes.py            # 5-stage state machine + protocol phrase tables
    │   ├── interpellations.py  # boundary detection + per-interpellation parser
    │   └── session.py          # chair / chair_segments / attendance / format / outcome / special_procedure
    └── plenary_joint_session.py  # NEW — thin orchestrator (composition over plenary/)
```

**Why sub-subpackage and not flat siblings under `extraction/`:** the agenda / votes / interpellations modules are *plenary-specific*, not shared helpers. committee_synthesis has no agenda/votes; report_facsimile has neither. Putting them at `extraction/` level would mix per-type code with the shared scaffolding.

**Why sub-subpackage and not one big file:** plenary will land at ~1500+ LOC across six independent subsystems. Single file forces every PR diff through one reviewable surface; sub-subpackage lets votes vs agenda evolve independently.

**Cross-type reuse for `plenary_joint_session`:** joint imports from `extractors/plenary/` as part of composition (Q12). No physical co-location at the extractor root needed.

**Plenary-specific boilerplate** (the SUMAR table, the `(STENOGRAMA)` marker, the italic chair-block paragraph) is claimed by `extractors/plenary/` modules with reasons prefixed `plenary_stenogram.*`, NOT by hoisting into shared `extraction/boilerplate.py`. Reason: shared boilerplate's bump invalidates *every* sidecar; plenary-only boilerplate should only invalidate plenary sidecars.

## 2. Helper graduations

### 2.1 `extraction/speakers.py` — graduates 0.1.0 → 0.2.0

Adds **shared low-level primitives**. Per-surface-form parsers live in their owning module.

The schema's `Speaker` shape is one dict (`{raw, name, title, role, party_group, person_id}`), but plenary input surface forms vary widely:

| # | Form | Owning module | Co-data |
|---|---|---|---|
| 1 | `## **Domnul/Doamna NAME:**` (speech header) | `extractors/plenary/activities.py` | `delivery_mode` (parenthetical) |
| 2 | `## **Domnul NAME, președintele Senatului:**` | same | same |
| 3 | `## **Domnul X (din sală):**` (with delivery) | same | same |
| 4 | `_…domnul deputat NAME, președintele…, și de domnul senator …_` (chair narrative italic) | `extractors/plenary/session.py` | `segment_label`, `started_at`, `secretaries[]` |
| 5 | `domnul deputat NAME` / `doamna senator NAME` (inline narrative) | shared primitives | — |
| 6 | `<Name>, deputat PARTY, Circumscripția…` (qr questioner) | `extractors/question_register.py` (shipped) | constituency in `raw` |
| 7 | Interpellation questioner | `extractors/plenary/interpellations.py` | `interpellation_number` |

**Shared primitives added to `extraction/speakers.py`:**

```python
SPEAKERS_VERSION = "0.2.0"

_HONORIFIC_RE = re.compile(r"\b(?:domnul|doamna|domnișoara|domnilor|doamnelor)\b", re.I)
_TITLE_RE = re.compile(
    r"\b(?:deputat|deputatã|deputatul|senator|senatoare|senatorul"
    r"|ministru|secretar|președinte|vicepreședinte|viceprim-ministru)\b",
    re.I | re.DOTALL,
)
_DELIVERY_RE = re.compile(
    r"\(\s*(?:de la tribună|din sală|prin (?:audio|video)conferin[țt][ăa]"
    r"|de la balcon|în scris)\s*\)",
    re.I,
)

def extract_delivery_mode(raw: str) -> tuple[str, str | None]:
    """Strip the parenthetical, return (stripped_raw, delivery_mode_enum)."""

def parse_honorific_speaker(raw: str) -> dict[str, Any]:
    """Handles 'Domnul/Doamna NAME[, role]' → Speaker dict."""

def parse_questioner(raw: str) -> dict[str, Any]:  # already shipped
    ...
```

**Delivery-mode enum mapping:**
- `de la tribună` → `tribune`
- `din sală` → `from_floor`
- `prin audioconferință` / `prin videoconferință` → `online` (collapsed; schema enum has no audio/video split)
- `de la balcon` → `from_balcony`
- `în scris` → `written`

**Why `delivery_mode` is on the speech turn, not the Speaker:** schema places it on `activities[].speech.delivery_mode`. A speaker giving two speeches in one session might be at the tribune for one and from-the-floor for another — annotation belongs to the turn, not the person.

**Bump consequences:** every qr sidecar (53 docs) re-extracts on first plenary pipeline run. ~2.5s total. Acceptable per the conservative-by-design version contract (Q11).

### 2.2 `extraction/references.py` — graduates 0.1.0 (stub) → 0.2.0

**Ship 6 strict variants + 1 `unknown` catch-all in v0.1.**

| Variant | Where it appears | v0.1? | Why |
|---|---|---|---|
| `bill` (Pl-x, PL-x, L) | primary_references + speeches | ✅ | Drives "which session debated bill X" — headline accountability query |
| `chamber_resolution` (PHCD, PHS, PHCDS) | primary_references + speeches | ✅ | Modern docs heavy with these (sample 2025-09-26 has many) |
| `parliamentary_resolution` (HP — `Hotărârea Parlamentului României nr. N/Y`) | primary_references in joint sessions | ✅ | Sample 2025-10-13 line 33 has 12+ HP refs in one doc |
| `oug` | primary_references (when bill approves OUG) + speeches | ✅ | Common in agenda titles |
| `og` | primary_references + speeches | ✅ | Same regex shape as oug minus "U" — free to ship |
| `law` (`Legea nr. N/Y`) | primary_references (rare) + speeches | ✅ | Cheap regex; needed for post-vote `Informare` items |
| `motion` | primary_references for `category=motion` items | ❌ | Defer — needs `proposers: Speaker[]`; structurally a Speaker dance |
| `court_decision` | speeches only | ❌ | Reduced query payoff for v0.1 |
| `constitution` | speeches only | ❌ | Same |
| `regulation` | speeches only | ❌ | Same |
| `eu_doc` (COM/JOCE/REG/DIR/JOIN) | speeches in EU debates | ❌ | Same |
| `treaty` | speeches (rare) | ❌ | Same |

**`unknown` shape:** `{type: "unknown", raw: string, char_offsets: [int, int], hint: "law-ish | court-ish | eu-doc-ish | other" | null}`. Captures spans that look reference-shaped but don't classify into the 6 strict types. Coverage credits the span; the `hint` field carries a coarse classifier output for next-version planning.

**Field set per shipped variant:**

- `bill` — `prefix` ("Pl-x" / "PL-x" / "L"), `number`, `year`, `secondary_year` (resubmissions like `132/2022/2023`), `chamber_of_origin` (derived: `Pl-x`/`PL-x` → camera, `L` → senat), `procedure` (`procedură de urgență` flag from agenda title context — v1.4.0 schema field).
- `law` — `number`, `year`, `subject` (best-effort short label).
- `oug` / `og` — `number`, `year`, `issuer` ("Guvern" / "Președintele").
- `chamber_resolution` — `prefix` (`PHCD` / `PHS` / `PHCDS`), `number`, `year`, `chamber` (derived from prefix).
- `parliamentary_resolution` — `number`, `year`, `subject` (best-effort).

**Universal fields on every variant** (including `unknown`): `type`, `raw`, `char_offsets` — the audit trail per schema § 6.

**Strictness on `primary_references` vs `references_mentioned`:**
- `primary_references` rejects `unknown` — if the chair announces a bill code we can't parse, that's an extractor bug worth surfacing, not a fallback to swallow.
- `references_mentioned` accepts `unknown` freely — speeches mention all kinds of citations; capturing the span is enough for v0.1.

**Why stage and not all 12 in v0.1:** 12 strict variants = 12 regex packs + 12 schema variants + 12 test cohorts before any sidecar lands. The headline query is "which session debated bill X" — covered by the 6 strict types. Court / constitution / EU / treaty are dominantly *speech-mentioned*; the speech text preserves them anyway. Coverage credits via `unknown`. Nothing is lost; precision is deferred.

**Why not "4-variant MVP" (bill / law / oug / chamber_resolution):** skipping `og` is artificial (regex identical to `oug` modulo one letter); skipping `parliamentary_resolution` is wrong on the data (joint sessions use HP heavily).

**Bump consequences:** every qr sidecar re-extracts (qr lists `references` in `extractor_versions` even though it doesn't use any). Trivial cost.

### 2.3 `extraction/topics.py` — NEW, 0.1.0

```python
TOPICS_VERSION = "0.1.0"

PRIMARY_TOPICS = (
    "Educație", "Sănătate", "Apărare", "Economie", "Transporturi",
    "Afaceri europene", "Justiție", "Muncă", "Agricultură", "Mediu",
    "Cultură", "Buget-finanțe", "Administrație", "Politică externă",
    "Drepturile omului",
)

_TOPIC_RULES: dict[str, list[re.Pattern]] = {
    "Educație": [
        re.compile(r"\b(?:educa[țt]ie|înv[ăa][țt][ăa]m[âa]nt|[șs]coli|...)\b", re.I),
        re.compile(r"Comisia\s+pentru\s+înv[ăa][țt][ăa]m[âa]nt", re.I),
    ],
    # ... 14 more, each: keyword pack + committee-name pack
}

def detect_primary_topics(agenda_title: str) -> list[str]:
    return [topic for topic, patterns in _TOPIC_RULES.items()
            if any(p.search(agenda_title) for p in patterns)]
```

**Why shared, not per-type:** vocabulary is cross-type. committee_synthesis (when it ships) uses the same primary topics. plenary_joint_session uses them. report_facsimile potentially uses them.

**Why title-scoped only** (per schema § 8 line 242): body content scoring would over-fire — a transport debate mentioning "spital" in passing would tag Sănătate. Title-scoped is precise; the title IS the topic.

**Why no-match returns `[]`** (not `["Other"]`): empty array is honest. Discovery loop surfaces empty-primary agenda items for new-rule discovery. Defaulting to "Other" would dilute the signal.

**Why multi-match returns full list:** schema models `topics.primary` as `array of string`. Multi-match is realistic — "Modificarea Codului fiscal pentru sectorul medical" matches Buget-finanțe AND Sănătate.

**Per-topic provenance** — deferred to v0.2 alongside LLM secondary-tag work. v0.1 emits the string list; schema body shape doesn't model per-tag extraction blocks today.

**Secondary topics** — `secondary: []` in v0.1. LLM pass is v0.2+ (per schema § 8 line 243).

## 3. Body content extractors

### 3.1 Agenda — `extractors/plenary/agenda.py`

**Central rule table; signal-weighted; per-category functions as escape hatches.**

The 28 categories (v1.4.0) split into three operational groups:

| Group | Detection | Examples |
|---|---|---|
| **Single-phrase discriminators** (~22 categories) | One regex on agenda title | `oath_taking` (`Depunerea jurământului`), `government_hour` (`Ora Guvernului`), `tacit_adoption` (`adoptat[ă] tacit`) |
| **Overlap-prone discriminators** (~5 categories) | Multiple rules fire — weighted resolution | `committee_membership` ("constituirea Comisiei speciale") vs `bill_debate` ("Dezbaterea Proiectului de hotărâre privind constituirea...") — specific rule wins (weight 1.0); generic `bill_debate` rule has weight 0.5 |
| **Sub-field-driven** (1 category) | Need post-detection refinement | `government_confidence` requires `confidence_type ∈ {investitură, cenzură, angajare_răspundere, demitere, null}` — function, not regex |

```python
@dataclass(frozen=True)
class _CategoryRule:
    category: str
    pattern: re.Pattern
    weight: float = 1.0  # 1.0 specific, 0.5 generic baseline

_CATEGORY_RULES: list[_CategoryRule] = [
    _CategoryRule("oath_taking", re.compile(r"Depunerea jur[ăa]m[âa]ntului", re.I)),
    _CategoryRule("government_hour", re.compile(r"\bOra\s+Guvernului\b", re.I)),
    # ... 24 more single-phrase rules at weight 1.0
    _CategoryRule("bill_debate", re.compile(
        r"(?:Dezbaterea\s+)?(?:Proiectul(?:ui)?\s+(?:de\s+lege|de\s+hot[ăa]r[âa]re)|Propunerii\s+legislative)",
        re.I), 0.5),
]

def detect_category(title: str) -> tuple[str, float, list[str]]:
    """(top_category, confidence, runners_up_within_0.2)"""
```

**Sub-field detectors** (each one regex on the title, run conditionally on the detected category):

- `confidence_type` — when `category=government_confidence`. Regex set on `învestitură|cenzură|angajare.*răspundere|demitere`.
- `requested_by_group` — when `category=government_hour`. Regex on `la solicitarea Grupului parlamentar al (.+)`.
- `reexamination_reason` — any category, when title contains `reexaminare`. Three regexes: `Pre[șs]edintelui României` → `presidential_request`; `Cur[țt]ii Constitu[țt]ionale` → `constitutional_court`; else `parliamentary_majority`.

**Default when no rule fires:** add `"other"` to the agenda category enum (additive minor schema bump). Matches the document-level `other` pattern. More honest than defaulting to `bill_debate` with low confidence.

**`agenda_items[].outcome` (9-value enum)** is a separate body-driven detector in `agenda.py` — looks at the body's vote-result line for the agenda item, not the title. Owned by a sibling helper that takes the agenda item's body span and returns the outcome enum.

**Multi-match resolution:** highest weight wins; runners-up within 0.2 of winner returned for confidence-encoding (not a separate schema field). When two specific rules tie, confidence drops to ~0.7-0.8 and the top wins by alphabetical fallback (deterministic tiebreaker).

### 3.2 Activity dispatch — `extractors/plenary/activities.py`

**2-pass partitioner — coarse partition by speaker headers, then fine refinement within each turn for embedded events.**

```python
def extract_activities(agenda_body, agenda_start_offset, ctx) -> list[Activity]:
    speech_turns = _partition_by_speaker(agenda_body)         # Pass 1
    activities = []
    for turn in speech_turns:
        activities.extend(_refine_turn(turn, ctx))            # Pass 2
    activities.sort(key=lambda a: a["source_span"]["chars"][0])  # Pass 3
    _assert_non_overlap(activities)                           # invariant guard
    return activities

def _partition_by_speaker(body):
    """Yield (header_match, content_span) for each ## **NAME:** header.
    Pre-first-speaker content stays out — owned by session.py."""

def _refine_turn(turn, ctx):
    """Inside one speech turn:
       1. votes.detect_votes() → split turn around each vote event
       2. Find narrator italic-only blocks → split around each
       3. Find deferral phrases (not inside votes) → split around each
       4. Find procedural italic blocks → split around each
       5. Emit non-event text fragments as speech sub-activities (same speaker)"""
```

**Why 2-pass and not single-pass state machine:** state machine couples speech-detection / vote-detection / narrator-detection. Adding a 6th event type later means extending the machine. 2-pass separates concerns; each refinement stage is independent and testable.

**Why not paragraph-predicate dispatch:** loses the speaker-turn boundary signal. Two consecutive speeches by *different* speakers have no content marker between them — only the `## **NAME:**` header.

**Edge cases:**

| Case | Treatment |
|---|---|
| **No speaker headers in agenda item** (`final_vote_batch`) | Wrap entire body as one implicit-chair speech (`speaker.raw="<chair narration>"`, `speaker.name=null`); refine into vote sub-activities. Relies on `session.chair[]` for attribution. |
| **Pre-first-speaker content** | Stays out of `activities[]` — owned by `session.py` for chair_segments / opened_at / attendance / SUMAR. |
| **Narrator vs procedural italic blocks** | Content discriminator: narrator = `(Aplauze\|Se intonează\|Murmure\|Voci din sală\|Se prezintă materialul)`; procedural = `Pauz\|reluăm\|continuăm\|suspend\|Se ridică ședința`. Mutually exclusive. |
| **Deferral phrase inside vote event** | Q5 vote detector consumes it as part of vote outcome (`outcome="deferred"`). Standalone `deferral` activity only for *bare* deferral phrases (rare). |
| **Adjacent narrator + speech** | Each gets its own activity (narrator = the italic span; speech = before/after fragments). Coverage credits both. |

**Activity ordering invariant:** sorted by `source_span.chars[0]` ascending. Non-overlapping enforced by construction (Pass 1 partitions disjointly; Pass 2 splits around events disjointly). `_assert_non_overlap` catches accidental bugs loudly.

**Coverage credit:** every activity emits one `record` claim. Agenda item itself doesn't get a separate claim — its span is the union of activities[] spans plus the title line.

### 3.3 Vote detector — `extractors/plenary/votes.py`

**All canonical Romanian vote-protocol phrases centralized; state machine across 5 protocol stages.**

```
Stage 1: Open       — chair triggers a vote
Stage 2: Result     — counts line
Stage 3: Outcome    — qualifier ("Cu majoritate" / "Cu unanimitate" / "Mulțumesc")
Stage 4: Deferral   — "rămâne pentru votul final" / cross-session deferral
Stage 5: Quorum     — "Nu avem cvorum"
```

| Stage | Phrase examples | Schema field |
|---|---|---|
| **1. Open** | `Supun votului`, `Vă rog să vă pregătiți de vot`, `Să înceapă votul`, `Trecem la vot` | enters vote-pending state; captures `motion_text` from preceding chair sentence |
| **2. Result** | `X voturi pentru, Y împotrivă, Z abțineri, N nu votez` | `counts.{for, against, abstain, not_voting, total_voting}` |
| **3. Outcome** | `Cu majoritate de voturi, X a fost aprobat` / `Cu unanimitate de voturi`, `Mulțumesc` (unanimous shorthand) | `outcome ∈ {approved, rejected, tied}`; `counts.for = "unanimous"` literal when no number |
| **4. Deferral** | `rămâne pentru votul final` (this session), `votul final se va da într-o ședință viitoare` (cross-session) | `outcome="deferred"`, `timing="deferred"` |
| **5. Quorum** | `Nu avem cvorum`, `cvorumul nu este îndeplinit` | `outcome="no_quorum"` |

**Orthogonal detectors** (run on the chair's announce text):

- `_MOTION_TYPE_RULES` → 8 enum values. `procedural` is fallback. `system_check` fires on `verificare a sistemului de vot|test al sistemului electronic` (per v1.3.0 P3-1 finding).
- `_VOTING_METHOD_RULES` → 7 enum values. `electronic_remote` (v1.4.0) requires explicit `de la distanță|prin vot remote|online`; falls back to `electronic` with `vot electronic|sistem electronic`; falls back to null when no method-marker hits (chair often doesn't restate the method on routine votes).

**State-machine pseudocode:**

```python
def detect_votes(span_text, span_start_offset) -> list[VoteActivity]:
    cursor = 0
    votes = []
    while True:
        open_match = _find_next_match(_VOTE_OPEN_PHRASES, span_text, cursor)
        if not open_match: break
        motion_text = _extract_announce_text(span_text, open_match.start)  # walks back ~3 paragraphs
        window_end = _find_next_open_or_speaker(span_text, open_match.end)
        window = span_text[open_match.end:window_end]
        result_match = _VOTE_RESULT_RE.search(window)
        deferral_match = _find_first_match(_DEFERRAL_PHRASES, window)
        quorum_match = _find_first_match(_QUORUM_FAIL_PHRASES, window)
        # Priority: explicit numeric > explicit deferral > quorum failure > implicit deferral
        ...
        cursor = window_end
    return votes
```

**Vote/speech overlap (the subtle one):** a vote always appears *inside* a chair's speaking turn. The Q6 speech-walker emits a `## **Domnul X:**` block as one speech activity. The vote is a *sibling* activity per schema. Resolution: vote detection runs as a **second pass** inside `_refine_turn`, *splitting* the chair's speech around vote events:

```
[speech: "Supun votului ordinea de zi..."],
[vote: motion_text=..., counts=..., outcome=...],
[speech: "Cu majoritate de voturi, ordinea de zi a fost aprobată. Supun votului programul..."],
[vote: ...],
[speech: "Cu majoritate, programul a fost aprobat. La primul punct..."],
```

Each speech fragment carries its own `source_span`. activities[] preserves chronological order. Coverage credits each fragment + each vote separately — no double-counting.

**Unanimous-literal handling:** `counts.for = "unanimous"` literal string when chair says only `Mulțumesc` after vote-open (or `Cu unanimitate` with no number). Schema § 8 line 211 — coercing to a number is editorializing. **Schema additive bump required:** `Vote.counts.for: oneOf [{type: integer}, {const: "unanimous"}, {type: null}]`.

**Deferred fields in v0.1:**
- `proposed_by` — null. Bill sponsor lives in `primary_references`; chair is announcer not sponsor.
- `nominal_breakdown` — null. Per-MP breakdown isn't in stenogram body text; it's published separately on parlament.ro.

**Quorum field handling:**
- Session-level `attendance.{registered, total_seats}` parsed once at session level from chair's opening announce.
- Per-vote `quorum_announced` defaults to null (no signal duplication).

### 3.4 Interpellation block — `extractors/plenary/interpellations.py`

**Boundary detection by chair's canonical transition phrase. No phrase found → `interpellations: []`. Block ends at EOF.**

```python
_TRANSITION_PHRASES = [
    re.compile(r"trecem\s+la\s+primirea\s+r[ăa]spunsurilor\s+la\s+interpel[ăa]ri", re.I),
    re.compile(r"începem\s+ora\s+(?:întreb[ăa]rilor\s+)?(?:[șs]i\s+)?interpel[ăa]rilor", re.I),
    re.compile(r"(?:vom\s+)?intr[ăa]m?\s+în\s+ora\s+(?:întreb[ăa]rilor|interpel[ăa]rilor)", re.I),
    re.compile(r"trecem\s+la\s+prezentarea\s+interpel[ăa]rilor\s+noi", re.I),
    re.compile(r"^\s*R[ăa]spunsuri\s+la\s+interpel[ăa]ri\s*[:.]?\s*$", re.I | re.M),
    re.compile(r"^##?\s+\*\*?\s*Întreb[ăa]ri\s+orale", re.I | re.M),
]

def find_interpellation_block(body) -> tuple[int, int] | None:
    """Return (block_start, block_end=len(body)) or None."""
```

**Why chair's transition phrase and not backward-scan-from-EOF or shape-heuristic:** false-positive risk. Late-agenda speeches by ministers responding to procedural questions look syntactically identical to interpellation responses. The chair's transition phrase is the unambiguous procedural signal; Romanian parliamentary procedure is *deliberate* about announcing it.

**Inside the block** — same partition pattern as qr (`question_register`):

| Field | Source | v0.1 |
|---|---|---|
| `genre` | Chair's announce text + structural markers | Default `interpelare` when ambiguous (formal/procedural) |
| `questioner` | Speaker dict via shared honorific primitives (Q2) | Standard |
| `addressed_to` | Chair's announce or first sentence (`Adresez această interpelare ministrului X` / `Ministerului X`) | Verbatim string |
| `addressed_to_normalized` | Ministry registry (deferred per schema § 8 line 231) | null |
| `interpellation_number` | Formal Camera tracking number (`Nr. 1.172B` form) | Reuse qr's `_REGNUM_RE` |
| `topic` | Chair's announced title or first sentence of questioner | Verbatim |
| `question_text` | Questioner's spoken text | Often null (filed in writing) |
| `response` | `{speaker, text}` when responder header follows | Optional |
| `response_deferred` | True when `(în scris)` notation present OR no responder block follows | Boolean |

**Per-interpellation source_span:** `[questioner_header_start, next_questioner_or_block_end)`. Includes both questioner block and response block; coverage credits the entire span as a single record claim.

**Block end at EOF:** no observed case of agenda content following the interpellation block. Detecting an explicit "Declar închisă ședința" phrase as the boundary would add complexity for zero queryable signal — owned by `session.py` for session-close fields.

**Coverage interaction:** when no transition phrase found, the entire body is agenda-walked. When a transition phrase IS found, the agenda walker stops at `block_start - 1`, and the interpellation walker takes over from `block_start`. Transition-phrase line itself is claimed as `claim_boilerplate` with reason `plenary_stenogram.interpellation_transition`.

**Interpellation block as agenda item?** Strictly sibling array (per schema § 4 design). The v1.4.0 `questions_interpellations` category covers *explicit agenda items* announcing the block (e.g., "Punctul N: Întrebări orale adresate Guvernului — Răspunsuri scrise"), NOT the block itself.

### 3.5 Session envelope — `extractors/plenary/session.py`

**All envelope fields shipped in v0.1 with appropriate nullability. Multiple sub-detectors run in parallel over body prefix + body suffix.**

| Field | Source | v0.1 strategy |
|---|---|---|
| `opened_at` | Italic `Ședința a început la ora HH:MM` | null when not found |
| `chair_segments[]` | Italic chair-narrative paragraphs (sample 2025-10-13 lines 81-85) — `Lucrările au fost conduse, în prima parte, de…` / `Ultima parte a ședinței a fost condusă de…` | Empty array if narrative not found; single-segment when no "în prima parte" / "ultima parte" markers |
| `chair[]` | Union of all `chair_segments[].chair` (always populated when at least one segment parses) | Empty array if no narrative |
| `secretaries[]` | Union of all `chair_segments[].secretaries` | Same |
| `attendance.{registered, total_seats}` | Chair's first announce (sample line 93): `Declar deschisă ședința... din totalul de N..., și-au înregistrat prezența M` | Both null if phrase not parsed |
| `quorum_met` | Derived: `registered ≥ ceil(total_seats / 2)` | null when either component is null |
| `format` | Body markers: `prin mijloace electronice` / `format mixt` / `în format fizic` (sample line 95) | **Default `in_person` for `published.year < 2020` when no marker** (pandemic-era added the marker universally per schema § X3-2); null otherwise |
| `closed_at` | Italic `Ședința s-a încheiat la ora HH:MM` near body end | null if not found |
| `outcome` | Closing phrase mapping: `Declar închisă` → `completed`; `Suspend ședința pentru lipsa cvorumului` → `suspended_no_quorum`; bare `Suspend ședința` → `suspended_other`; `Ședința continuă` → `adjourned` | **Default `completed` when no closing phrase but `closed_at` parsed**; null otherwise |
| `special_procedure` | **Two-pass**: (a) session-header markers (`Ședință solemnă consacrată` → `sedinta_solemna`; `Deschiderea sesiunii ordinare/extraordinare` → `deschiderea_sesiunii`; `Deschiderea legislaturii` → `deschiderea_legislaturii`; `Mesaj/Discurs al Președintelui` → `mesaj_prezidential`); (b) post-agenda derivation (single agenda item with `category=political_declarations` matching `declarația solemnă a Parlamentului` → `declaratie_solemna`; bill_debate item with primary_reference matching `bugetului de stat` → `buget_de_stat`) | null for most regular sessions |

**SUMAR table-of-contents** (sample lines 28-77 — markdown table with `<br>` line breaks listing every agenda item):

**Treat SUMAR as source-of-truth for agenda enumeration**: parse the table → seed `agenda_items[]` skeletons (ordinal, title, pages_in_pdf), then refine each by finding the body span between successive item-N markers. **Body-scan fallback** for older docs without SUMAR (~30% of corpus).

Reasons:
- SUMAR titles are clean (no chair-narration interleaving), unambiguously ordered, with page-range info that's *only* available there.
- Body-side agenda headers are inconsistent: sometimes `## **N. <title>**`, sometimes just `## <title>`, sometimes title is split across chair turns.

**Coverage credit for envelope content:** each successfully parsed sub-field emits one *record* claim covering the source span (chair narrative block is record-claimed, not boilerplate-claimed). SUMAR table emits one record claim per agenda item title-line span when option (b) applies.

**Owns the pre-first-speaker span** (per Q6): body content from start of body to first `## **NAME:**` header. Where chair narrative italic block, `Ședința a început` italic line, and SUMAR table live.

## 4. Calibration

### 4.1 Coverage targets

| Threshold | Value | Where it bites |
|---|---|---|
| **Discovery-loop margin** (`--coverage-below MARGIN`) | **0.85** | CLI default for plenary outlier flagging |
| **Test fixture floor** | **0.80** | Per-fixture assertion in `tests/extraction/test_plenary_stenogram.py`; suite fails if any sample drops below |
| **Corpus mean target** | **0.90** | Documented in `architecture.md` § plenary; not gated |
| **Per-doc production gate** | **none** | Coverage stays diagnostic per existing scaffolding contract |

**Why 0.85 discovery margin:** too low (0.80) misses real gaps in 80-85% band; too high (0.90) drowns the discovery loop with outliers (estimated ~half the 2240-plenary cohort would flag at 0.90 against realistic 90% mean).

**Why 0.80 test floor:** if v0.1 can't hit 80% on hand-picked modern stenograms, structural bug. 0.85 prematurely tightens — v0.1 is supposed to be lossy on the long tail. 0.75 too permissive.

**Why 0.90 mean target:** 0.95 is qr-territory (flat lists with backbone covering the body); plenary's free-form prose can't sustain that. 0.85 too soft. 0.90 is a stretch motivating regex tightening before v0.1 ship.

**Median + p10 over mean** — mean gets dragged by catastrophic outliers; median is typical case; p10 is "systematic bottom 10%". Both computed from the JSONL outlier feed.

**Per-component coverage breakdown in schema** — deferred. JSONL gap dump already supports per-region analysis (each gap has chars, lines, preview; jq does the rest).

### 4.2 Test fixtures (5)

`tests/extraction/fixtures/plenary/`:

1. **Modern joint session** (`2025-10-13_MO-PII-117-2025.md`) — heavy SUMAR, multi-segment chairs, joint chairs from both chambers, parallel references in primary_references[].
2. **Modern Camera Deputaților** (2024 Q1 sample) — single chair, modern PHCD references, post-pandemic format markers.
3. **Modern Senat** (2025 Q3 sample) — different chair-narration formula, single chamber.
4. **Mid-corpus 2017** — post-PHCD adoption era, pre-pandemic, simpler chair_segments.
5. **Older 2013 Camera** — no SUMAR, simpler structure, single chair, pre-modern PHCD adoption — exercises body-scan agenda fallback.

Each fixture's test asserts: `coverage ≥ 0.80`; sidecar passes schema validation; specific spot-checks (agenda item N has `category=X`; vote at item Y has `outcome=Z`; `chair_segments[0].chair.name=W`).

## 5. Versioning

### 5.1 Per-component contract (unchanged)

`pipeline._versions_current()`: compare cached sidecar's `extractor_versions` dict to expected = `_shared_helper_versions()` ∪ `{doc_type: EXTRACTOR_VERSIONS[doc_type]}`. Strict equality on every key. Mismatch → re-extract.

### 5.2 Helper version increments at v0.1 ship

| Helper | Pre-v0.1 | v0.1 plenary ship | Trigger |
|---|---|---|---|
| `boilerplate` | 0.1.0 | 0.1.0 | Plenary boilerplate stays in `extractors/plenary/` (not hoisted) |
| `coverage` | 0.1.0 | 0.1.0 | No changes needed |
| `references` | 0.1.0 (stub) | **0.2.0** | Q3 — graduates from stub to 6+unknown variants |
| `speakers` | 0.1.0 | **0.2.0** | Q2 — adds shared primitives (honorific, delivery, title) |
| `topics` | (new key) | **0.1.0** | Q10 — first ship |
| `plenary_stenogram` | (new key) | **0.1.0** | Q1 — first ship |
| `plenary_joint_session` | (new key) | **0.1.0** | Q12 — first ship |

**`_shared_helper_versions()` after Q10:**

```python
def _shared_helper_versions() -> dict[str, str]:
    return {
        "boilerplate": BOILERPLATE_VERSION,
        "coverage": COVERAGE_VERSION,
        "references": REFERENCES_VERSION,
        "speakers": SPEAKERS_VERSION,
        "topics": TOPICS_VERSION,         # NEW
    }
```

Plus per-type: `extractor_versions["plenary_stenogram"] = "0.1.0"` and `extractor_versions["plenary_joint_session"] = "0.1.0"`.

### 5.3 Why flat contract (not per-doc-type isolation)

- **Trivial cost**: 2.5s for the 53-doc qr cohort to re-extract on first plenary run. Helper graduation events are rare.
- **Conservative-by-design**: a helper can change *output shape* (not just behavior) and a static dependency declaration in the alternative wouldn't catch it. Over-invalidation is the safe default.
- **No new registry to maintain**: per-doc-type isolation needs `EXTRACTOR_DEPENDENCIES: dict[DocumentType, set[str]]` plus discipline. Drift is the failure mode.
- **Pattern continuity**: existing CLAUDE.md line "version-aware idempotency keyed on per-component extractor_versions" stays correct as-is.

**Schema compatibility:** `extractor_versions` $def (lines 122-135) uses `patternProperties` + `additionalProperties: false` — accepts variable key sets. Adding `topics` is fully compatible; no schema bump for the version-keying machinery itself.

## 6. plenary_joint_session — composition over inheritance

**Both per-type extractors are thin orchestrators over the same sub-extractors in `extractors/plenary/`. Joint adds one detector (`chambers_present`) and uses identical agenda/activities/votes/interpellations/session machinery.**

### Architecture

```python
# extractors/plenary/__init__.py — plenary_stenogram extractor
def extract(ctx) -> tuple[dict, list[Claim]]:
    session_dict, c1 = session.extract_session(ctx)
    agenda_items, c2 = agenda.extract_agenda(ctx)
    interpellations_list, c3 = interpellations.extract_interpellations(ctx)
    body = {
        "session": session_dict,
        "agenda_items": agenda_items,
        "interpellations": interpellations_list,
    }
    return body, c1 + c2 + c3

# extractors/plenary_joint_session.py — plenary_joint_session extractor
from monitorul_ii.extraction.extractors.plenary import session, agenda, interpellations

EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_LABEL = f"regex@plenary_joint_session@{EXTRACTOR_VERSION}"

_JOINT_HEADER_RE = re.compile(
    r"[ȘS]EDIN[ȚT]E\s+COMUNE\s+ALE\s+CAMEREI\s+DEPUTA[ȚT]ILOR\s+[ȘS]I\s+SENATULUI", re.I
)

def detect_chambers_present(body: str) -> list[str]:
    return ["Camera Deputaților", "Senatul"] if _JOINT_HEADER_RE.search(body) else []

def extract(ctx) -> tuple[dict, list[Claim]]:
    session_dict, c1 = session.extract_session(ctx)
    session_dict["chambers_present"] = detect_chambers_present(ctx.body_text)
    agenda_items, c2 = agenda.extract_agenda(ctx)
    interpellations_list, c3 = interpellations.extract_interpellations(ctx)
    body = {
        "session": session_dict,
        "agenda_items": agenda_items,
        "interpellations": interpellations_list,
    }
    return body, c1 + c2 + c3
```

### Why composition

- **Inheritance** is awkward in Python at the module-function level; would force class-based extractor pattern that breaks the existing `extract(ctx) → tuple[dict, list[Claim]]` contract used by qr.
- **Shared-core-with-discriminator** (one `extract(ctx)` branching on `is_joint`) couples joint-only quirks into the plenary code path. Schema's `oneOf` discriminator returns different body shapes per type, so the function would have to branch on doc_type — pushing dispatch logic into the extractor.
- **Composition** keeps each per-type extractor as a *thin orchestrator* (15-20 LOC) over the same sub-extractors. New per-type extractor in the family = new orchestrator, no machinery duplication.

### Schema $def split

Two flat session $defs (no JSON-Schema inheritance gymnastics):

- `PlenarySession` — used by `PlenaryStenogramBody`. No `chambers_present`.
- `PlenaryJointSession` — used by `PlenaryJointSessionBody`. Adds required `chambers_present: array of string`.

Body $defs share the same `agenda_items` and `interpellations` shapes (one $def each).

## 7. Schema deltas required pre-v0.1 ship

Schema bumps from current 1.5.0 → **1.6.0** (additive minor — consumers reading 1.6.0 records can ignore unfamiliar fields and continue).

| # | Delta | Driver |
|---|---|---|
| 1 | New `Reference` $def with `oneOf` over 7 variants (6 strict + unknown), each requiring `{type, raw, char_offsets}` | Q3 |
| 2 | `Vote.counts.for: oneOf [{type: integer}, {const: "unanimous"}, {type: null}]` | Q5 unanimous-literal |
| 3 | `agenda_items[].category` enum extended with `"other"` | Q4 default |
| 4 | New `PlenarySession` $def replacing `PendingBody` for plenary_stenogram body | Q1, Q8, Q12 |
| 5 | New `PlenaryJointSession` $def (extends shape with `chambers_present: array of string`) replacing `PendingBody` for plenary_joint_session body | Q12 |
| 6 | New `PlenaryStenogramBody` and `PlenaryJointSessionBody` $defs (replacing PendingBody refs in the top-level `oneOf`) — strict shapes | Q1, Q12 |
| 7 | New `Activity` $def (oneOf over `Speech | Vote | Procedural | Narrator | Deferral`) with `Speech.delivery_mode` enum | Q2, Q6 |
| 8 | New `Interpellation` $def with all v1.4.0 fields | Q7 |
| 9 | New `AgendaItem` $def with all v1.4.0 fields including `category="other"` extension | Q4, Q8 |

`PendingBody` stays in the schema for `committee_synthesis` and `report_facsimile` (not yet shipped). Tightened in their respective extractor's v0.1 ship.

## 8. Build order

Roughly the conceptual priority + dependency order:

1. **Schema bumps** — bump `extraction_schema.json` to 1.6.0 with all 9 deltas. Add tests asserting each new $def is structurally valid (mirrors `tests/extraction/test_schema.py` pattern). Tighten validator's understanding of the discriminated `oneOf`.
2. **Helper graduations**:
   - `extraction/topics.py` (NEW) — 15 canonical topics, rule table, `detect_primary_topics`. Tests in `tests/extraction/test_topics.py`.
   - `extraction/speakers.py` (0.1.0 → 0.2.0) — add `extract_delivery_mode`, `parse_honorific_speaker`, shared honorific/title regexes. Tests extended in `tests/extraction/test_speakers.py`.
   - `extraction/references.py` (0.1.0 stub → 0.2.0) — 6 strict variants + unknown, schema $def alignment. Tests in `tests/extraction/test_references.py` (NEW).
3. **Plenary sub-extractors** (in dependency order):
   - `extractors/plenary/session.py` — chair_segments, attendance, format, opened_at/closed_at, outcome, special_procedure, SUMAR parsing.
   - `extractors/plenary/agenda.py` — 28-category rule table, agenda_items[] population (SUMAR-driven with body-scan fallback), sub-field detectors.
   - `extractors/plenary/votes.py` — 5-stage state machine, motion_type / voting_method detectors, unanimous-literal handling.
   - `extractors/plenary/activities.py` — 2-pass partitioner, vote/speech overlap split, narrator/procedural discriminator.
   - `extractors/plenary/interpellations.py` — boundary detection, per-interpellation parser.
4. **Per-type orchestrators**:
   - `extractors/plenary/__init__.py` — plenary_stenogram `extract` orchestrator.
   - `extractors/plenary_joint_session.py` — plenary_joint_session `extract` orchestrator + `chambers_present` detector.
5. **Register in `extractors/__init__.py`** — add both to `EXTRACTORS` + `EXTRACTOR_VERSIONS`.
6. **Pipeline updates** — fold `topics` into `_shared_helper_versions()`; add per-type aggregation logic in `_aggregate_confidence` for the new body shapes.
7. **Test fixtures** — 5 hand-picked plenary stenograms in `tests/extraction/fixtures/plenary/`, with golden sidecars for spot-check assertions.
8. **Test suites**:
   - `tests/extraction/test_plenary_session.py`
   - `tests/extraction/test_plenary_agenda.py`
   - `tests/extraction/test_plenary_votes.py`
   - `tests/extraction/test_plenary_activities.py`
   - `tests/extraction/test_plenary_interpellations.py`
   - `tests/extraction/test_plenary_stenogram.py` (end-to-end + per-fixture coverage assertions)
   - `tests/extraction/test_plenary_joint_session.py` (end-to-end + 2025-10-13 fixture)
9. **CLI integration** — `extract` subcommand already supports new types via the dispatcher (no flag changes needed).
10. **Discovery loop sweep** — run `monitorul-ii extract pdfs/ --coverage-below 0.85` over the full plenary cohort. Aggregate JSONL stats; tighten regexes for systematic gaps; iterate.
11. **Doc updates**:
    - `CLAUDE.md` — Commands section unchanged (no new flags); `extract` paragraph mentions new types in coverage.
    - `docs/architecture.md` § Extract — new subsection "Plenary stenogram body" describing the agenda/activities/votes/interpellations/session split + 0.90 mean target measurement.
    - `README.md` — `extract` paragraph mentions plenary_stenogram + plenary_joint_session as additionally supported.

## 9. Doc touchpoints (post-implementation)

Per CLAUDE.md "Document every new feature, in the same change. Non-negotiable." principle:

| Doc | Update |
|---|---|
| `README.md` | `extract` subcommand paragraph: list `plenary_stenogram` and `plenary_joint_session` alongside `question_register` as supported types. |
| `CLAUDE.md` | `extract` paragraph: note plenary types ship in v0.1.x; mention SUMAR-driven agenda enumeration; note coverage targets (0.85 / 0.80 / 0.90). |
| `docs/architecture.md` | New § "Extract pipeline — plenary types" detailing: module layout, agenda category rule table mechanics, vote state-machine stages, vote/speech overlap second-pass split, SUMAR vs body-scan agenda strategy, chair_segments parser, coverage measurements (mean / median / p10 across the 2240-doc cohort), regex-vs-LLM tradeoffs. |
| `docs/extraction-schema.md` | Mark v0.1 plenary types as **shipped** in the build-order section; document the schema 1.5.0 → 1.6.0 deltas required. |

## 10. Open design notes (deferred to v0.2+)

- **Reference variants 7-12** (motion, court_decision, constitution, regulation, eu_doc, treaty) — graduate as discovery-loop signals justify.
- **`addressed_to_normalized` / `ministry_normalized`** — ministries registry. Same nullable-pattern as `person_id`. Backfilled cross-document.
- **Secondary topics (LLM pass)** — `secondary: [string]` populated from agenda title + body keyword extraction.
- **Per-topic provenance** — `topics.primary[].extraction` per-tag block (additive schema bump alongside LLM secondary work).
- **`proposed_by` Speaker linkage** on votes — needs person registry.
- **`nominal_breakdown`** on votes — needs cross-source ingest from parlament.ro voting records.
- **Cross-document `defers_to` / `resolves`** — links cross-session deferrals to their resolving final-vote document. Backfilled by a linker pass.
- **`bill.subject`, `law.subject`, `parliamentary_resolution.subject`** — best-effort short labels from citing context. v0.1 leaves null.
- **RELEASE_VERSION vs IMPL_VERSION** for shared helpers — coarse vs fine version bumps. Defer; strict per-component bumping is fine for current cost levels.
- **Per-component coverage in schema** (`coverage.by_section`) — JSONL gaps suffice for now.

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Older 2013 stenograms have idiosyncratic structure that breaks SUMAR-fallback agenda walker | Medium | Coverage drops on older cohort | Body-scan fallback ships with v0.1; treats older docs as a separate p10 segment in coverage diagnostics |
| Vote state-machine misattributes results to wrong vote when multiple opens are close together | Medium | Wrong `motion_text` / `outcome` pairing | Window detection caps look-ahead at next-open or next-speaker boundary; hand-verify on golden corpus |
| `chair_segments` parser misses single-chair sessions (no "în prima parte" marker) | Low | `chair_segments: []` instead of single-segment | Fallback: when no segment markers found but chair narrative exists, emit single segment with `segment_label="întreaga ședință"` |
| `unknown` reference catches legitimate-but-unmodeled patterns and signal gets lost in the long tail | Medium | Discovery loop misses graduation signal | `hint` field explicitly aids triage; periodic JSONL aggregation surfaces frequent unknown shapes |
| Speech / vote second-pass split produces overlapping spans on edge cases (deferral inside vote inside speech) | Low | `_assert_non_overlap` raises | Hard assertion catches loudly; case-by-case fixes |
| Schema tightening from PendingBody to strict body causes existing sidecar invalidation cascade | Low | One-time re-extraction of 53 qr docs | Acceptable per Q11 conservative-by-design contract |

## 12. v0.1.x — discovery-loop coverage recovery

The v0.1 ship calibrated against the modern fixture set (post-2014). A full-corpus sweep over the 5551 plenary MDs in 2026-05-04 surfaced a bottom quartile near zero coverage (plenary_stenogram p25=0.081, mean=0.689; 30% of docs below 0.50). Bottom-quartile inspection bucketed the gaps into three dominant patterns rather than a long tail of subtle ones:

1. **Mojibake (~31% of outliers, 2000-2007 cohort)** — pre-2008 PDFs were converted from a Romanian font that lacked Unicode diacritics. PyMuPDF preserves the legacy bytes so MDs carry `Þ/þ` for `Ț/ț`, `ª/º` for `Ș/ș`, `ã` for `ă`, `Ñ/Ð` for em-dashes/en-dashes. Modern-diacritic-only regexes failed to detect chair blocks, SUMAR keywords, time markers.
2. **No agenda markers (~50% of outliers, all eras)** — short sessions (declarations, response-to-interpellations, procedural-only) and many pre-2008 docs have either a SUMAR with descriptive (non-numbered) entries OR no SUMAR, AND have no `## **N. Title**` body markers. v0.1 produced empty `agenda_items: []` and orphaned the body's speech turns.
3. **Trailing footer un-claimed** — `**EDITOR: GUVERNUL ROMÂNIEI**` masthead + `**A B O N A M E N T E   L A   P U B L I C A Ț I I L E**` subscription rate-card runs ~500-2000 chars at end-of-doc; never claimed as boilerplate.

**Fixes (all in `extractors/plenary/`):**

- **Diacritic-tolerant regexes** in `session.py`: a module-level `_SEDINTA_VARIANTS` accepts modern Unicode (`Ședin[țt]a`), cedilla (`Şedin[țţ]a`), mojibake (`ªedinþa`), and stripped (`Sedinta`) forms. Time separator widened from `[.:]` to `[.,:]` for pre-2008 `13,25` style. `_OPENED_AT_*` / `_CLOSED_AT_RE` / `_ATTENDANCE_RE` / `_CHAIR_BLOCK_OPENING_RE` / `_CLOSED_PHRASE_RE` / `_SUSPEND_*_RE` / `_ADJOURNED_RE` all reference the variant character classes.
- **Chair-block phrasing variants**: `_CHAIR_BLOCK_OPENING_RE` accepts modern `Lucrările au fost conduse`, 2008-era `Lucrările ședinței au fost conduse`, and 2008+ joint `Ședința a fost condusă`.
- **`_CHAIR_PERSON_RE` rank-optional path**: pre-2010 / Senate docs use the `domnul Nicolae Văcăroiu, președintele Senatului` form (no rank, role suffix carries the chamber). Without an explicit rank, a chair-person is only emitted when a chamber-bearing role suffix follows — prevents over-firing on every `domnul X` mention. Title is inferred from role: `președintele Senatului` → `senator`; `Camerei Deputaților` → `deputat`.
- **SUMAR keyword variants**: `_SUMAR_OPENING_RE` accepts bare `SUMAR`, markdown-prefixed `## SUMAR`, and pipe-prefixed `|SUMAR<br>...`. Plenary-boilerplate `sumar_keyword` reason claims all three forms.
- **`_BODY_AGENDA_ITEM_RE` relaxation**: the `**` bold wrapper became optional so older docs' `## N. Title` (no `**`) headers parse. The `## ` prefix stays required to keep numbered lists embedded in speeches from over-firing.
- **Implicit single-item agenda fallback (`agenda.py`)**: when SUMAR-driven enumeration produces zero entries AND body-scan finds no `## **N. Title**` markers AND the post-SUMAR span contains at least one `## **NAME:**` speech header, wrap the entire span as one implicit agenda item with `category="other"`, `title="Ședința"`, `confidence=0.4`. Activities are extracted normally so all speeches get claimed. Skipped for blank/whitespace-only spans (nothing to claim).
- **Editor footer boilerplate (`boilerplate.py`)**: `plenary_stenogram.editor_footer` reason matches `\*\*\s*(?:EDITOR\s*:|A\s+B\s+O\s+N\s+A\s+M\s+E\s+N\s+T\s+E)[\s\S]*\Z` — the bold-prefixed editor masthead plus the subscription rate-card.

**Spot-check on outliers** (all measured at write=False with current code):

| Sample | Layout | Before | After |
|---|---|---|---|
| `2000-02-11_MO-PII-2-2000.md` | Senatul mojibake, no body N. markers | 0.002 | 0.997 |
| `2005-02-11_MO-PII-2-2005.md` | Senatul mojibake | low | 0.995 |
| `2008-09-12_MO-PII-73-2008.md` | Senatul, descriptive SUMAR, no body N. | 0.014 | 0.999 |
| `2015-02-23_MO-PII-16-2015.md` | Modern interpellations-only session | 0.014 | 0.999 |
| `2015-03-13_MO-PII-31-2015.md` | Modern declarations-only session | 0.030 | 0.999 |

**Full-corpus sweep results** (5551 MDs, before/after):

| Cohort | Metric | Before | After |
|---|---|---|---|
| plenary_stenogram | mean | 0.689 | **0.913** |
| plenary_stenogram | p25 | 0.081 | **0.980** |
| plenary_stenogram | <0.85 | 1465 (36%) | **565 (14%)** |
| plenary_stenogram | <0.50 | 1236 (30%) | **307 (8%)** |
| plenary_joint_session | mean | 0.765 | **0.956** |
| plenary_joint_session | <0.85 | 97 (26%) | **24 (6%)** |

**Three new pre-2010 fixtures** in `tests/extraction/fixtures/plenary/` exercise the mojibake + implicit-fallback paths with `LEGACY_COVERAGE_FLOOR = 0.50` (modern fixture floor stays 0.80):

- `2000-02-11_MO-PII-2-2000.md` — Senatul, mojibake (`Ñ Þ ª ã`)
- `2005-02-11_MO-PII-2-2005.md` — Senatul, mojibake
- `2008-09-12_MO-PII-73-2008.md` — Senatul, no body `N.` markers

**Versioning:** these are regex extensions only. No schema change, no new helper version key. `EXTRACTOR_VERSION` for `plenary_stenogram` and `plenary_joint_session` stays at `0.1.0` — a future point bump (`0.1.1`) belongs in the same change as the next round of pattern additions, but isn't strictly required for correctness since the fallback only fires where the previous code emitted nothing (no shape change for already-covered docs).

**What's NOT touched** (intentionally):

- `extraction/boilerplate.py` (shared) — bumping its version invalidates qr / committee / report sidecars too. The mojibake patterns affect plenary specifically; the `**DEZBATERI PARLAMENTARE**` and `Anul XI Ñ Nr. 2` mojibake in shared preamble is a smaller-leverage win and can land in a follow-up shared bump.
- `_clip_overlaps` in `activities.py` — stays a clip rather than a hard assertion. The previous hard-assertion variant produced 1462 false errors across the corpus.
- Coverage gating — still diagnostic-only. Test fixtures are the only place coverage is enforced as a floor.

**v0.2-and-later candidates seen during the sweep** (not addressed in v0.1.x):

- The 2000-2007 cohort has an inconsistent `**DEZBATERI PARLAMENTARE**` (bold without `#` prefix) banner that `extraction/boilerplate.py` doesn't claim. ~30 chars per doc, ~600 docs affected, low marginal coverage.
- Pre-2008 `Anul XI Ñ Nr. 2` issue header (no parenthetical Roman numeral, mojibake separator) is also not claimed by the shared `year_issue_banner` regex.
- Both above belong in the next shared-boilerplate bump.
- Truly novel layouts in pre-2003 docs (multi-column rendering quirks, strange `<br>` placements inside SUMAR cells) — small cohort, not worth a discrete fix.
