"""Discourse-analysis enrichment producer (Hawkins / voice / DQI / V-Party).

Walks each sidecar's substantive speeches (`is_substantive` text_length
≥ 100, canonical-speaker only, ≤ MAX_WORDS), runs the four-prompt
LLM pipeline against OpenRouter (default model: Gemini 3.1 Flash-Lite),
and persists the structured outputs as

    <basename>.discourse.flash-lite.v0_1.json

next to the sidecar. The indexer's enrichment loader picks up the file,
the denormaliser flattens the per-record payload into the per-grain
`mo-speeches.enrichments.discourse.*` namespace, and the search layer
serves the new fields automatically (Q3 / Q5 of `docs/elasticsearch-indexing.md`,
plus `docs/discourse-pilot-baseline-2026-05.md` § 11 for the four-cell
cross-tab design).

Pipeline order per speech: Hawkins → voice (conditional) → DQI → V-Party.
The voice pass only runs when Hawkins emits at least one marker; DQI and
V-Party are independent classifiers (no marker dependency on Hawkins) but
kept sequential at v0.1 for simplicity and per-call rate-limit headroom.
v0.2 may parallelise the three independent passes.

Idempotency contract: each entry stores a `text_fingerprint` (12-char
sha256 of the NFC + whitespace-collapsed speech text). On re-run,
fingerprint-matched entries reuse the existing payload verbatim — no
HTTP call, no file rewrite. Mismatched fingerprints trigger re-coding
for that record only.

Long-tail handling (v0.1): speeches above MAX_WORDS (default 800) are
SKIPPED with reason `text_too_long`. v0.2 will introduce chunked coding
with `record_id#chunk-N` keys (mirroring the embedding producer's
deferred chunking path).

Why a separate module from the smoke harness (tools/pilot_benchmark.py):
the harness is a discrete A/B / calibration tool that walks
`validation/*.jsonl` inputs against the model registry; the producer
walks the actual sidecar corpus and persists outputs alongside the
sidecars. Shared primitives — schema-aware OpenRouter calls with the
strict→json_object fallback, json-repair parsing, evidence offset
recovery via `str.find` — are lifted from the harness verbatim because
they encode lessons from the calibration sweeps (Gemini's strict-schema
rejection, Sonnet's unescaped-quote-inside-rationale failure mode, etc.).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading as _threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

# ---------------------------------------------------------------------------
# Producer identity
# ---------------------------------------------------------------------------

# Filename + indexer dict key — matches the loader's regex
# (<basename>.<producer>(.<model>)?.v<version>.json) so the merge step
# picks up payloads automatically. The two-segment filename pattern
# (`discourse.flash-lite` rather than just `flash-lite`) lets future
# discourse producers (e.g. opus-escalation hybrid) coexist under the
# same `enrichments.discourse` namespace.
DISCOURSE_NAMESPACE = "discourse"  # filename + indexer dict key
DISCOURSE_PRODUCER = "flash-lite"  # filename + `_meta.producer`
DISCOURSE_VERSION = "0_1"  # filename version (underscore form)
DISCOURSE_VERSION_DOTTED = "0.1"  # `_meta.version` (dotted form)
DISCOURSE_MODEL = "google/gemini-3.1-flash-lite"  # OpenRouter slug

# Provider names. The producer can route LLM calls to either OpenRouter
# (the default; OpenAI-compatible API at /chat/completions; auth via
# Bearer header; cost in `usage.cost`) or Google AI Studio (the native
# Gemini API at /v1beta/models/{model}:generateContent; auth via `?key=`
# query param; cost computed locally from token counts).  Each provider
# has its own URL + key env var + healthcheck endpoint + request/response
# shape; the dispatcher (`call_llm`) wraps the differences so the rest
# of the producer code is provider-agnostic.
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_GOOGLE = "google"
SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_OPENROUTER, PROVIDER_GOOGLE)
DEFAULT_PROVIDER = PROVIDER_OPENROUTER

# Per-provider default model. OpenRouter uses prefixed slugs
# (`google/gemini-3.1-flash-lite`); Google's native API takes the bare
# model name. Overridable per call via the `model=` parameter.
DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    PROVIDER_OPENROUTER: "google/gemini-3.1-flash-lite",
    PROVIDER_GOOGLE: "gemini-3.1-flash-lite",
}

# Pinned prompt versions per the calibration baseline. Bump in lockstep
# with `prompts/<name>_v<N>.{md,schema.json}` revisions; the producer
# stamps `_meta.prompt_versions` on every entry so re-runs after a
# prompt bump can detect stale payloads via per-record diff.
PROMPT_VERSIONS: dict[str, str] = {
    "hawkins": "v1",
    "voice": "v1",
    "dqi": "v1",
    "vparty": "v2",
}

# OpenRouter Flash-Lite per-token rates (USD). Sourced from the
# calibration smoke (~$0.00493 / speech × 4-prompt pipeline → tuned to
# the conservative $0.10/M input + $0.40/M output bound). Operator can
# override after observing the actual invoice; the `--budget-usd` cap
# uses these rates to estimate cumulative spend.
OPENROUTER_FLASH_LITE_INPUT_RATE_USD = 0.10 / 1_000_000  # $/input token
OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD = 0.40 / 1_000_000  # $/output token

# Google AI Studio (native Gemini API) per-token rates (USD). Direct
# Google pricing is typically lower than OpenRouter's marked-up rate
# because there's no margin layer. We use conservative estimates: input
# $0.075/M, output $0.30/M. Real costs are billed per Google's tier
# (free tier free up to rate limits; paid tier per these rates). Native
# API does NOT return cost in the response — these constants drive the
# `--budget-usd` estimate. Operator can tune in this file after seeing
# their actual Google Cloud invoice.
GOOGLE_FLASH_LITE_INPUT_RATE_USD = 0.075 / 1_000_000
GOOGLE_FLASH_LITE_OUTPUT_RATE_USD = 0.30 / 1_000_000

# Records below this many chars don't get coded — they're chair turns
# ("Mulțumesc", "Vă rog") whose discourse signal is below the rubric's
# resolution. Mirrors the indexer's `is_substantive` cutoff
# (`SUBSTANTIVE_TEXT_LENGTH = 100` in `denormalize.py`).
MIN_TEXT_CHARS = 100

# Speeches above this many words get SKIPPED with reason `text_too_long`.
# v0.2 will introduce chunked coding with `record_id#chunk-N` keys.
# 800 words ≈ 6,000 chars at the corpus's token-density — the upper end
# of what fits comfortably in the four-prompt pipeline at the chosen
# `max_tokens` ceiling.
DEFAULT_MAX_WORDS = 800

# Output-token ceiling per LLM call. Sized against the production
# distribution: across 45,189 calls in the first sweep, p50=1488,
# p95=2352, p99=2979, max=18493. The distribution is bimodal — 99%
# of calls finish under 3K tokens, then a tiny tail of runaway-marker
# DQI calls hits the cap exactly. The previous cap of 16384 turned
# those 3 records (0.01%) into truncated `'rationale' is a required
# property` failures because the model emits properties in schema
# order and DQI's `rationale` is the last required field; runaway
# `markers[]` exhausted the budget before reaching it. 32768 gives
# 2× headroom over those observed runaway cases without affecting the
# typical-call cost (the model emits what it needs, not the cap; cost
# is per-token-actually-output, not per-cap). Reasoning tokens count
# toward the API's `completion_tokens` total but NOT toward
# `max_tokens` on Google's native API — that's why an actual count of
# 16778 was observed under a 16384 cap.
DEFAULT_MAX_TOKENS = 32768

# Default endpoint. Override per-call via `openrouter_url` argument or
# via the `OPENROUTER_URL` environment variable. The CLI also reads
# `OPENROUTER_API_KEY` from the environment (auto-loaded via dotenv).
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"

# Google AI Studio default endpoint (native Gemini API, NOT the
# OpenAI-compatible bridge at `/v1beta/openai`). Override per-call via
# `base_url` argument or via the `GOOGLE_AI_STUDIO_API_URL` env var.
# Auth is via `?key=<API_KEY>` query string; the CLI reads
# `GOOGLE_AI_STUDIO_API_KEY` from the environment.
DEFAULT_GOOGLE_AI_STUDIO_URL = "https://generativelanguage.googleapis.com/v1beta"

# Flush the discourse JSON file every N successfully-coded records
# within a sidecar (rather than writing once at end-of-sidecar). Bounds
# the worst-case data loss on Ctrl+C / kill / power loss to N records
# per active worker. Each flush is an atomic .part-rename; the cost of
# doing 20× more writes per long sidecar is negligible (sub-millisecond
# at filesystem level vs ~3 seconds per LLM call). N=5 means at -j 36
# you lose at most ~180 in-memory records on a hard exit, vs ~1,800
# without periodic flushing.
PARTIAL_FLUSH_EVERY = 5

# Non-canonical speaker labels that surface as Speaker.name / .raw and
# represent procedural / institutional voices, not individuals. Skipped
# from coding entirely — these aren't graded by Hawkins / V-Party
# rubrics. Lifted verbatim from the persons-registry denylist so the
# producer's filter matches the matcher's homonym-disambiguation guard.
NON_CANONICAL_SPEAKERS: frozenset[str] = frozenset(
    {
        "<chair narration>",
        "Din sală",
        "Din sala",
        "Voci",
        "Voci din sală",
        "Voci din sala",
        "Guvernul",
        "Aplauze",
        "Rumoare",
    }
)

# OpenRouter / OpenAI Structured Outputs reject substrings that signal
# the chosen backend doesn't honour strict json_schema response_format —
# we fall back to json_object on these (the schema is already inlined
# in the prompt body, so semantic enforcement happens via the prompt +
# json-repair downstream). Verbatim from the pilot harness.
_STRICT_SCHEMA_REJECTION_SUBSTRINGS: tuple[str, ...] = (
    "json_schema",
    "response_format",
    "structured output",
    "structured_output",
    "not support",
    "is not supported",
    "unrecognized",
    "unsupported",
    "invalid schema",
)

# Order matters — voice depends on hawkins (uses its emitted markers as
# input). DQI and V-Party are independent classifiers (no marker
# dependency on Hawkins).
PROMPT_SEQUENCE: tuple[str, ...] = ("hawkins", "voice", "dqi", "vparty")

# Prompt + schema files. Same source-of-truth as the pilot harness.
_PROMPT_FILES: dict[str, tuple[str, str]] = {
    "hawkins": (
        "prompts/hawkins_populism_v1.md",
        "prompts/hawkins_populism_v1.schema.json",
    ),
    "voice": (
        "prompts/voice_classifier_v1.md",
        "prompts/voice_classifier_v1.schema.json",
    ),
    "dqi": (
        "prompts/dqi_v1.md",
        "prompts/dqi_v1.schema.json",
    ),
    "vparty": (
        "prompts/vparty_antipluralism_v2.md",
        "prompts/vparty_antipluralism_v2.schema.json",
    ),
}


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    """One round-trip to the LLM for one prompt on one speech.

    Mirrors the harness's CallResult so per-record `_meta.errors` can
    surface configuration / transport / parse failures alongside the
    structured output. `cost_usd` is OpenRouter's authoritative cost
    when populated (via `usage: {include: true}`); otherwise a per-
    token estimate that undercounts hidden reasoning tokens.
    """

    prompt_kind: str
    output: dict[str, Any] | None
    error: str | None
    error_detail: str | None
    fragments_not_found: list[str]
    parse_repaired: bool
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int
    rate_limit_retries: int = 0
    fallback_to_json_object: bool = False


@dataclass
class AnalyzeResult:
    """Outcome of one `analyze_sidecar(...)` call.

    `action`:
      - `"analyzed"` — at least one record was freshly coded; the file
        was rewritten.
      - `"skipped"` — every record already had an up-to-date payload;
        no file write.
      - `"dry-run"` — `dry_run=True` short-circuited before write/HTTP.
      - `"error"` — sidecar-level failure (parse, missing document_id);
        see `errors`.
      - `"budget-exhausted"` — the cumulative-spend estimate hit the
        `--budget-usd` cap mid-sidecar; the producer finished the
        in-flight record atomically and stopped.

    `analyzed` is the count of records freshly coded this run; `reused`
    is the count of fingerprint-matched entries kept verbatim;
    `skipped` is the count of records dropped by the substantive /
    canonical-speaker / max-words filters.
    """

    sidecar_path: Path
    document_id: str
    action: str
    analyzed: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0  # records that hit errors and were NOT persisted
    file_path: Path | None = None
    cost_estimate_usd: float = 0.0
    rate_limit_retries: int = 0
    fallback_count: int = 0
    repaired_count: int = 0
    # Diagnostic counters for the live progress bar.
    call_count: int = 0  # total LLM round-trips this sidecar (incl. retries)
    tokens_in_total: int = 0
    tokens_out_total: int = 0
    hawkins_with_markers: int = 0  # speeches where Hawkins emitted ≥1 marker
    hawkins_no_markers: int = (
        0  # speeches where Hawkins emitted 0 markers (voice skipped)
    )
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Filename / basename helpers (mirror embedding.py)
# ---------------------------------------------------------------------------


def discourse_filename(basename: str) -> str:
    """Compose the canonical discourse filename for a sidecar basename.

    Result: `<basename>.discourse.flash-lite.v0_1.json`.
    """
    return f"{basename}.{DISCOURSE_NAMESPACE}.{DISCOURSE_PRODUCER}.v{DISCOURSE_VERSION}.json"


def _resolve_basename(sidecar_path: Path) -> str:
    name = sidecar_path.name
    if name.endswith(".extraction.json"):
        return name[: -len(".extraction.json")]
    return sidecar_path.stem


def _normalise_text(text: str) -> str:
    """NFC + whitespace-collapsed text used for fingerprinting + as the
    speech excerpt sent to the model. Identical normalisation across
    the two ensures a fingerprint match guarantees the coded text was
    the same text.
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFC", text)
    return " ".join(norm.split())


def _text_fingerprint(text: str) -> str:
    """sha256(NFC + whitespace-collapsed text), hex-truncated to 12 chars.

    Same shape as the identity layer's `compute_content_fingerprint` and
    the embedding producer's fingerprint — deliberately reused so the
    producer's idempotency contract aligns with the rest of the pipeline.
    """
    digest = hashlib.sha256(_normalise_text(text).encode("utf-8")).hexdigest()
    return digest[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _word_count(text: str) -> int:
    return len(text.split())


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write to `<path>.part` then rename — same atomic-rename pattern
    used by extract / link / backfill / embed. Producers must never leave
    a half-written enrichment file on disk where the indexer might pick
    it up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


_log_lock = _threading.Lock()


class JsonlLogger:
    """Thread-safe per-speech JSONL appender.

    Writes one JSON line per coded record. Each line is a complete JSON
    object terminated by `\\n` so consumers can `tail -f` the file and
    parse each line independently. The lock serialises writes across
    `-j N` worker threads — without it, two threads can interleave a
    single line's bytes and produce malformed JSON.

    The append-mode `open(...)` is held only for the duration of one
    write; long-running runs don't accumulate file handles. POSIX
    guarantees that small (<PIPE_BUF, typically 4 KB) write(2) calls
    are atomic at the kernel level even without the lock — but the
    lock is cheap insurance against larger entries (DQI rationale +
    error strings can exceed 4 KB).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Touch so consumers can `tail -f` even before the first write
        # lands (otherwise tail polls a non-existent inode).
        path.touch(exist_ok=True)

    def write(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with _log_lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")


# ---------------------------------------------------------------------------
# Speech walker
# ---------------------------------------------------------------------------


@dataclass
class _SpeechTask:
    """One record selected for coding."""

    record_id: str
    text: str  # NFC + whitespace-collapsed; the version sent to the model
    fingerprint: str


def _is_canonical_speaker(speaker: dict[str, Any]) -> bool:
    """True when the speaker dict represents a canonical individual.

    Drops chair-narration / Din sală / Voci / Guvernul-style procedural
    voices that aren't graded by the Hawkins / V-Party rubrics.
    """
    if not isinstance(speaker, dict):
        return False
    name = (speaker.get("name") or "").strip()
    raw = (speaker.get("raw") or "").strip()
    for label in (name, raw):
        if label and label in NON_CANONICAL_SPEAKERS:
            return False
    return True


def _iter_speech_tasks(
    sidecar: dict[str, Any],
    *,
    max_words: int,
) -> tuple[list[_SpeechTask], int]:
    """Walk every speech activity and yield `(tasks, skipped_count)`.

    Filters: speech-type + canonical-speaker + len(text) ≥ MIN_TEXT_CHARS
    + word-count ≤ max_words. Records below MIN_TEXT_CHARS or non-
    canonical are dropped silently — they're not analysis-eligible. The
    skipped counter tracks speeches above max_words (the long-tail
    deferral) so the operator can audit how often v0.2's chunking would
    fire.
    """
    tasks: list[_SpeechTask] = []
    skipped = 0
    body = sidecar.get("body") or {}
    if not isinstance(body, dict):
        return tasks, skipped
    for item in body.get("agenda_items") or []:
        if not isinstance(item, dict):
            continue
        for act in item.get("activities") or []:
            if not isinstance(act, dict):
                continue
            if act.get("type") != "speech":
                continue
            rid = act.get("id")
            if not rid:
                continue
            speaker = act.get("speaker") or {}
            if not _is_canonical_speaker(speaker):
                continue
            text = act.get("text") or ""
            if not isinstance(text, str):
                continue
            normalised = _normalise_text(text)
            if len(normalised) < MIN_TEXT_CHARS:
                continue
            if _word_count(normalised) > max_words:
                skipped += 1
                continue
            tasks.append(
                _SpeechTask(
                    record_id=rid,
                    text=normalised,
                    fingerprint=_text_fingerprint(normalised),
                )
            )
    return tasks, skipped


# ---------------------------------------------------------------------------
# Prompt loading (cache once per process)
# ---------------------------------------------------------------------------

_PROMPT_CACHE: dict[str, tuple[str, dict[str, Any]]] | None = None


def _prompt_root() -> Path:
    """Locate the `prompts/` directory relative to the project root.

    The prompt files live at `<repo-root>/prompts/`; we walk up from
    this module's file path until we hit the repo root (the parent of
    `src/`).
    """
    p = Path(__file__).resolve()
    # .../monitorul/src/monitorul_ii/extraction/enrichments/discourse.py
    # → .../monitorul/
    return p.parents[4]


def _load_prompts(
    *, prompts_dir: Path | None = None
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Load the four prompt + schema files; cache for the process lifetime.

    `prompts_dir` is an override hook for tests / non-default layouts.
    Default looks under `<repo-root>/prompts/`.
    """
    global _PROMPT_CACHE
    if _PROMPT_CACHE is not None and prompts_dir is None:
        return _PROMPT_CACHE
    root = prompts_dir if prompts_dir is not None else _prompt_root()
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, (prompt_rel, schema_rel) in _PROMPT_FILES.items():
        prompt_path = (
            root / prompt_rel if not prompts_dir else (root / Path(prompt_rel).name)
        )
        schema_path = (
            root / schema_rel if not prompts_dir else (root / Path(schema_rel).name)
        )
        # When prompts_dir is set, expect flat layout (prompts_dir/<filename>);
        # otherwise expect repo layout (root/prompts/<filename>).
        if prompts_dir is not None:
            prompt_path = prompts_dir / Path(prompt_rel).name
            schema_path = prompts_dir / Path(schema_rel).name
        prompt_text = prompt_path.read_text(encoding="utf-8")
        schema_obj = json.loads(schema_path.read_text(encoding="utf-8"))
        out[kind] = (prompt_text, schema_obj)
    if prompts_dir is None:
        _PROMPT_CACHE = out
    return out


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def build_prompt_for_speech(
    prompt_text: str, speech_excerpt: str, schema: dict[str, Any]
) -> str:
    """Compose the user message for hawkins / dqi / vparty (single-speech input)."""
    schema_block = json.dumps(schema, ensure_ascii=False, indent=2)
    return f"""{prompt_text}

---

## Speech to classify

```
{speech_excerpt}
```

## Required output

Return ONE JSON object that conforms to the following JSON Schema. No prose, no commentary, no markdown fences — just the JSON.

```json
{schema_block}
```
"""


# Typographic glyphs that fold to ASCII for tolerant matching. The model
# routinely paraphrases evidence.text by swapping curly quotes for ASCII,
# normalising em/en-dashes, joining lines (whitespace) — none of which is
# real content drift, just typography. Folding these recovers ~95% of the
# previously-failed voice runs on Flash-Lite output.
_DOUBLE_QUOTE_GLYPHS = "„“”«»"  # „ " " « »
_SINGLE_QUOTE_GLYPHS = "‘’‚‛"  # ' ' ‚ ‛
_DASH_GLYPHS = "‐‑‒–—―"  # ‐ ‑ ‒ – — ―
_ELLIPSIS_GLYPH = "…"  # …


def _normalise_for_match(s: str) -> tuple[str, list[int], list[int]]:
    """Return `(normalised, src_starts, src_ends)` where each character
    `i` of `normalised` corresponds to the source slice
    `s[src_starts[i]:src_ends[i]]` in the ORIGINAL string.

    The maps let a successful match in normalised space project back to
    byte-correct offsets into the original string. Folds applied:

      - NFC normalisation (combining marks collapsed to single codepoints).
      - Lowercase (case is typography, not content).
      - Whitespace runs (incl. newlines) → single ASCII space.
      - Typographic double-quotes (",",„,«,») → ASCII '"'.
      - Typographic single-quotes (','‚‛) → ASCII "'".
      - Dashes (‐,‑,‒,–,—,―) → ASCII '-'.
      - Ellipsis '…' → ASCII '...'.

    All folds preserve positional information: an ellipsis character
    yields three normalised dots that all map back to the same source
    slice, so a match ending on the ellipsis still produces a correct
    end offset in the original string. Per-char `lower()` is 1:1 for the
    Romanian + extended Latin subset; the rare multi-char lower (ß→ss
    etc.) is collapsed back to a single char to keep the map invariant.
    """
    s = unicodedata.normalize("NFC", s)
    out: list[str] = []
    src_starts: list[int] = []
    src_ends: list[int] = []
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if ch.isspace():
            run_start = i
            while i < n and s[i].isspace():
                i += 1
            # Suppress leading whitespace runs so the match position
            # at the start of the normalised string corresponds to the
            # first non-space char.
            if out:
                out.append(" ")
                src_starts.append(run_start)
                src_ends.append(i)
            continue
        if ch in _DOUBLE_QUOTE_GLYPHS:
            out.append('"')
            src_starts.append(i)
            src_ends.append(i + 1)
        elif ch in _SINGLE_QUOTE_GLYPHS:
            out.append("'")
            src_starts.append(i)
            src_ends.append(i + 1)
        elif ch in _DASH_GLYPHS:
            out.append("-")
            src_starts.append(i)
            src_ends.append(i + 1)
        elif ch == _ELLIPSIS_GLYPH:
            for _ in range(3):
                out.append(".")
                src_starts.append(i)
                src_ends.append(i + 1)
        else:
            lowered = ch.lower()
            # Multi-char lowercase (e.g. ß→ss) would break the 1:1 map;
            # take the first char so the offsets stay byte-correct. The
            # corner case is exotic for Romanian corpus and the result
            # is still a folded form for matching purposes.
            out.append(lowered[0] if lowered else ch)
            src_starts.append(i)
            src_ends.append(i + 1)
        i += 1
    # Strip a trailing collapsed-whitespace token if present so the
    # last char of the normalised string corresponds to a real char.
    while out and out[-1] == " ":
        out.pop()
        src_starts.pop()
        src_ends.pop()
    return "".join(out), src_starts, src_ends


def find_text_offsets(speech_text: str, fragment: str) -> list[int] | None:
    """Locate `fragment` in `speech_text`; return `[start, end)` or None.

    Strict byte-exact match (kept for callers that want the strict
    signal — e.g. the per-call `fragments_not_found` telemetry). For
    matching that tolerates whitespace + typography drift, use
    `find_text_offsets_tolerant`.
    """
    idx = speech_text.find(fragment)
    if idx < 0:
        return None
    return [idx, idx + len(fragment)]


def find_text_offsets_tolerant(speech_text: str, fragment: str) -> list[int] | None:
    """Locate `fragment` in `speech_text` with typography-tolerant matching.

    Returns `[start, end)` into the ORIGINAL `speech_text` on success.
    Tries byte-exact first (the fast, common case); on miss, normalises
    both strings (`_normalise_for_match`) and retries the search in
    normalised space, projecting the match back through the offset map
    so the returned range is byte-correct.

    Tolerates: whitespace runs (newlines / tabs / multiple spaces folded
    to a single space), case differences (a sentence-leading `Un` vs
    `un` quoted by the model), Romanian typographic quotes (`„ " " « »`
    → `"`; `' ' ‚ ‛` → `'`), unicode dashes (`‐ ‑ ‒ – — ―` → `-`),
    and the ellipsis glyph (`…` → `...`).

    Real paraphrase (added/removed words, swapped synonyms, dropped
    punctuation) still misses, which is correct: the matcher is for
    typography drift, not semantic recovery.
    """
    if not fragment:
        return None
    idx = speech_text.find(fragment)
    if idx >= 0:
        return [idx, idx + len(fragment)]
    speech_norm, src_starts, src_ends = _normalise_for_match(speech_text)
    if not speech_norm:
        return None
    frag_norm, _, _ = _normalise_for_match(fragment)
    if not frag_norm:
        return None
    norm_idx = speech_norm.find(frag_norm)
    if norm_idx < 0:
        return None
    norm_end = norm_idx + len(frag_norm)
    return [src_starts[norm_idx], src_ends[norm_end - 1]]


def build_prompt_for_voice(
    prompt_text: str,
    speech_excerpt: str,
    markers: list[dict[str, Any]],
    schema: dict[str, Any],
) -> str:
    """Compose the voice-classifier user message: speech + per-marker payload.

    Offsets for each marker's `evidence.text` are recovered via the
    typography-tolerant matcher (`find_text_offsets_tolerant`). When
    even tolerant matching fails — meaning the model paraphrased the
    quote rather than swapping typography — the marker is STILL
    included in the payload, with `char_range` omitted: voice can
    classify from `marker_text` alone (the voice prompt explicitly
    says hints are signals, not commitments). Pre-fix, such markers
    were dropped, and a marker-list of all-paraphrased entries
    returned `""` causing the voice pass to silently skip and the
    speech to land in the run's `failed` bucket.
    """
    marker_payload: list[dict[str, Any]] = []
    for i, m in enumerate(markers):
        # Defensive: a non-dict marker (e.g. malformed model output where
        # the salvage couldn't recover) must not crash the voice pass —
        # skip it and let the rest classify. The validation gate in
        # `_run_pipeline_for_speech` should have already filtered these
        # out, but guarding here keeps the function safe for any caller.
        if not isinstance(m, dict):
            continue
        text = (m.get("evidence") or {}).get("text") or ""
        if not text:
            continue
        entry: dict[str, Any] = {"marker_id": f"m_{i}", "marker_text": text}
        offsets = find_text_offsets_tolerant(speech_excerpt, text)
        if offsets is not None:
            entry["char_range"] = offsets
        marker_payload.append(entry)
    if not marker_payload:
        return ""
    voice_input = {
        "speech_excerpt": speech_excerpt,
        "pre_detected_regions": [],
        "markers": marker_payload,
    }
    schema_block = json.dumps(schema, ensure_ascii=False, indent=2)
    input_block = json.dumps(voice_input, ensure_ascii=False, indent=2)
    return f"""{prompt_text}

---

## Input

```json
{input_block}
```

## Required output

Return ONE JSON object that conforms to the following JSON Schema, with one classification entry per input marker (in input order). No prose, no commentary, no markdown fences — just the JSON.

```json
{schema_block}
```
"""


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------


def _filter_invalid_markers(obj: Any, schema: dict[str, Any]) -> tuple[Any, int]:
    """Drop entries from `obj["markers"]` that fail per-item schema
    validation. Returns `(possibly-modified-obj, drop_count)`.

    Recovers from any per-marker drift mode the model produces:
      - Bad enum values in `kind` (e.g. voice-classifier values
        `quoted` / `reported` cross-contaminating Hawkins).
      - Missing required fields inside a marker (e.g. DQI emitting a
        marker without `preliminary_voice`).
      - Wrong-typed sub-fields (e.g. `evidence` as a bare string instead
        of an object — model collapses one nesting level).
      - Non-dict marker entries (stray strings produced by structural
        confusion).

    This is a strict generalisation of the previous kind-enum-only
    salvage: every previously-recovered shape is still recovered here,
    plus the long tail of one-off per-marker drift. Cheap on the success
    path — only runs from `parse_and_validate`'s exception handler.
    """
    import jsonschema

    if not isinstance(obj, dict):
        return obj, 0
    markers = obj.get("markers")
    if not isinstance(markers, list):
        return obj, 0
    items_schema = (schema.get("properties") or {}).get("markers", {}).get("items")
    if not isinstance(items_schema, dict):
        return obj, 0
    validator = jsonschema.Draft202012Validator(items_schema)
    cleaned: list[Any] = []
    dropped = 0
    for m in markers:
        try:
            validator.validate(m)
        except jsonschema.ValidationError:
            dropped += 1
            continue
        cleaned.append(m)
    if dropped == 0:
        return obj, 0
    return {**obj, "markers": cleaned}, dropped


def _strip_unknown_properties(obj: Any, schema: dict[str, Any]) -> tuple[Any, int]:
    """Recursively strip object keys not declared in the schema's
    `properties` map when the object's schema sets
    `additionalProperties: false`. Returns `(possibly-new-obj, strip_count)`.

    Recovers from the model inventing extra fields — common Gemini
    drift modes seen in production: a top-level
    `respect_for_constructive_politics` (hybrid of `respect_for_*` and
    `constructive_politics`), `text_2` / `text_3` evidence keys, etc.
    Strict validation rejects the whole payload; stripping the unknown
    keys preserves the rest of the response.

    Walks `properties` and `items` (for arrays); also descends into the
    object-typed branches of `anyOf` so the function works on the
    `evidence: anyOf [null, object]` shape used by some markers.
    """
    if not isinstance(obj, dict) or not isinstance(schema, dict):
        return obj, 0
    stripped = 0
    declared_props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        unknown = [k for k in obj if k not in declared_props]
        if unknown:
            obj = {k: v for k, v in obj.items() if k not in unknown}
            stripped += len(unknown)
    # Recurse into each declared property.
    for key, sub_schema in declared_props.items():
        if key not in obj or not isinstance(sub_schema, dict):
            continue
        value = obj[key]
        new_value, more = _walk_for_strip(value, sub_schema)
        if more:
            obj = {**obj, key: new_value}
            stripped += more
    return obj, stripped


def _walk_for_strip(value: Any, schema: dict[str, Any]) -> tuple[Any, int]:
    """Descend through a (sub-schema, value) pair, applying
    `_strip_unknown_properties` to objects and walking into arrays /
    `anyOf` branches that include an object type.
    """
    total = 0
    candidate_schemas: list[dict[str, Any]] = [schema]
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        for branch in any_of:
            if isinstance(branch, dict):
                candidate_schemas.append(branch)
    if isinstance(value, dict):
        for cs in candidate_schemas:
            value, more = _strip_unknown_properties(value, cs)
            total += more
        return value, total
    if isinstance(value, list):
        items_schema = schema.get("items")
        # `anyOf`-wrapped arrays: pick whichever branch declares items.
        if not isinstance(items_schema, dict):
            for branch in candidate_schemas:
                if isinstance(branch.get("items"), dict):
                    items_schema = branch["items"]
                    break
        if isinstance(items_schema, dict):
            new_list = []
            for item in value:
                new_item, more = _walk_for_strip(item, items_schema)
                new_list.append(new_item)
                total += more
            if total:
                return new_list, total
        return value, 0
    return value, 0


def parse_and_validate(
    raw_text: str, schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None, bool]:
    """Strip optional code fences, parse JSON (with json-repair fallback),
    validate against `schema` (with two schema-aware salvage passes on
    initial validation failure).

    Returns `(output, error_class, error_detail, repaired)`. `error_class`
    is `None | "json_parse" | "schema_invalid"`. `repaired=True` signals
    a self-heal happened — either json-repair recovered a malformed JSON
    string (typically an unescaped ASCII `"` inside a Romanian-quoted
    string) OR schema validation initially failed but the salvage passes
    (`_filter_invalid_markers` + `_strip_unknown_properties`) produced a
    structurally valid output. The two recovery modes share
    the `repaired` flag because both surface to the operator as
    `repaired=N` in the per-call telemetry — the operator just wants to
    know how often self-heal fires; the kind of heal is debug-only and
    inspectable via `--explain`.

    The salvage passes are deliberately conservative: they only DROP
    things (markers with cross-contaminated enum values, unknown object
    properties). They never invent missing required fields — the model's
    most common drift modes are over-emission (extra keys, wrong-enum
    markers), not under-emission, so dropping is a strict improvement.
    """
    import jsonschema

    text = (raw_text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    repaired = False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as strict_exc:
        try:
            from json_repair import repair_json
        except ImportError:
            return None, "json_parse", str(strict_exc), False
        try:
            repaired_text = repair_json(text)
            obj = json.loads(repaired_text)
            repaired = True
        except (json.JSONDecodeError, ValueError) as repair_exc:
            return (
                None,
                "json_parse",
                f"strict: {strict_exc}; repair: {repair_exc}",
                False,
            )
    try:
        jsonschema.validate(obj, schema)
        return obj, None, None, repaired
    except jsonschema.ValidationError as initial_exc:
        # Salvage pass 1: strip object properties not declared in the
        # schema (Gemini invents `respect_for_constructive_politics`,
        # `text_2` inside evidence, etc.). Run BEFORE the marker filter
        # so a marker with one extra evidence key gets cleaned up
        # rather than dropped entirely.
        salvaged, stripped = _strip_unknown_properties(obj, schema)
        # Salvage pass 2: drop markers that fail per-item schema
        # validation AFTER strip. Covers bad-enum kind values (voice
        # classifier's `quoted` leaking into Hawkins), missing required
        # fields inside a marker (DQI sometimes omits
        # `preliminary_voice`), wrong-typed sub-fields (evidence emitted
        # as bare string instead of object), and stray non-dict marker
        # entries.
        salvaged, dropped_markers = _filter_invalid_markers(salvaged, schema)
        if dropped_markers == 0 and stripped == 0:
            return obj, "schema_invalid", initial_exc.message, repaired
        try:
            jsonschema.validate(salvaged, schema)
        except jsonschema.ValidationError as exc2:
            # Salvage didn't help — surface the post-salvage error so
            # the operator sees what's still wrong (e.g. a required
            # field the model never emitted; can't be fabricated).
            return salvaged, "schema_invalid", exc2.message, repaired
        # Salvage succeeded. Mark `repaired=True` so the per-call
        # `repaired_count` bumps and the operator sees we self-healed.
        return salvaged, None, None, True


def _collect_fragments_not_found(
    output: dict[str, Any] | None, speech_text: str
) -> list[str]:
    """Walk the output for any `evidence.text` / `voice_evidence.text`
    that can't be located in the speech text.
    """
    if not isinstance(output, dict):
        return []
    misses: list[str] = []

    def _check(text_value: str | None) -> None:
        # Tolerant matcher so the telemetry surfaces only REAL
        # paraphrase (added/removed words). Whitespace + typography
        # drift is not a "miss" — it's recoverable.
        if text_value and find_text_offsets_tolerant(speech_text, text_value) is None:
            misses.append(text_value)

    markers = output.get("markers") or []
    if isinstance(markers, list):
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            evidence = marker.get("evidence") or {}
            if isinstance(evidence, dict):
                _check(evidence.get("text"))
    classifications = output.get("classifications") or []
    if isinstance(classifications, list):
        for entry in classifications:
            if not isinstance(entry, dict):
                continue
            ev = entry.get("voice_evidence")
            if isinstance(ev, dict):
                _check(ev.get("text"))
    return misses


# ---------------------------------------------------------------------------
# OpenRouter adapter (httpx-direct, OpenAI-compatible)
# ---------------------------------------------------------------------------


def _looks_like_schema_rejection(detail: str) -> bool:
    msg = detail.lower()
    return any(s in msg for s in _STRICT_SCHEMA_REJECTION_SUBSTRINGS)


def _post_chat_completion(
    client: httpx.Client,
    *,
    openrouter_url: str,
    api_key: str,
    body: dict[str, Any],
) -> httpx.Response:
    return client.post(
        openrouter_url.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
    )


def _build_request_body(
    *,
    model: str,
    full_prompt: str,
    schema: dict[str, Any] | None,
    max_tokens: int,
    use_strict_schema: bool,
) -> dict[str, Any]:
    schema_format: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": (schema.get("$id") if isinstance(schema, dict) else None)
            or "output",
            "strict": True,
            "schema": schema,
        },
    }
    response_format = (
        schema_format if use_strict_schema and schema else {"type": "json_object"}
    )
    # `usage: {include: true}` makes OpenRouter return the actual
    # upstream cost in `usage.cost` (USD). Without this flag, providers
    # like Gemini bury reasoning tokens / cache costs / margin in the
    # provider-side billing and the standard `prompt_tokens` /
    # `completion_tokens` undercount badly (smoke run on 504 records:
    # producer estimated $1.95, OpenRouter actually charged ~$11 →
    # 5.6× under-estimate). The flag is OpenRouter-specific; harmless
    # on other backends that ignore unknown fields.
    return {
        "model": model,
        "messages": [{"role": "user", "content": full_prompt}],
        "response_format": response_format,
        "temperature": 0,
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }


# Rate-limit handling. OpenRouter caps Flash-Lite at 200–450 req/min
# (varies with provider load); 429s arrive as transport errors that
# need their own backoff strategy — the per-request retry budget
# (`--retry-on-error`) is for transport / parse failures and uses
# 1–8s exponential backoff which is the wrong shape for a per-minute
# rate window. We absorb 429s INSIDE `call_openrouter` with a separate
# budget so the producer-level retry budget stays focused on
# substantive failures.
RATE_LIMIT_MAX_RETRIES = 6  # 6 attempts per call before giving up
RATE_LIMIT_BACKOFF_SCHEDULE = (15.0, 30.0, 60.0, 90.0, 120.0, 120.0)  # seconds
RATE_LIMIT_MAX_BACKOFF = 120.0  # cap on Retry-After header values


def _parse_retry_after(headers: Any, status_code: int) -> float | None:
    """Pull seconds from the `Retry-After` header if present.

    Per RFC 7231 the header may be either a delta-seconds integer or
    an HTTP-date. We accept the integer form (OpenRouter's standard);
    HTTP-date form would need parsing and isn't worth the complexity
    given the schedule fallback below.
    """
    if status_code != 429:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if val is None:
        return None
    try:
        s = float(val)
        return min(s, RATE_LIMIT_MAX_BACKOFF)
    except (TypeError, ValueError):
        return None


def _post_with_rate_limit_handling(
    client: httpx.Client,
    *,
    openrouter_url: str,
    api_key: str,
    body: dict[str, Any],
    rate_limit_retries: int = RATE_LIMIT_MAX_RETRIES,
) -> tuple[httpx.Response | None, int, str | None]:
    """POST to /chat/completions, absorbing 429s with exponential backoff.

    Returns `(response, rate_retries_used, error_detail)`. If every
    rate-limit retry is exhausted, response is the LAST 429 (caller
    surfaces it as a transport error). Other transport errors (network,
    timeout) propagate via httpx.HTTPError to the caller.

    Rate limits are absorbed silently from the producer's perspective —
    the caller doesn't see them as errors unless the budget is fully
    exhausted. This matches what users want: a sustained run shouldn't
    fail records over transient rate-limit windows.
    """
    last_resp: httpx.Response | None = None
    last_detail: str | None = None
    for attempt in range(rate_limit_retries + 1):
        resp = client.post(
            openrouter_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code != 429:
            return resp, attempt, None
        # Rate-limited. Compute backoff from Retry-After header (preferred)
        # or fall back to the schedule.
        last_resp = resp
        last_detail = resp.text[:200]
        if attempt >= rate_limit_retries:
            break
        retry_after = _parse_retry_after(resp.headers, resp.status_code)
        if retry_after is None:
            schedule_idx = min(attempt, len(RATE_LIMIT_BACKOFF_SCHEDULE) - 1)
            sleep_for = RATE_LIMIT_BACKOFF_SCHEDULE[schedule_idx]
        else:
            sleep_for = retry_after
        time.sleep(sleep_for)
    return last_resp, rate_limit_retries, last_detail


def call_openrouter(
    *,
    client: httpx.Client,
    openrouter_url: str,
    api_key: str,
    model: str,
    full_prompt: str,
    schema: dict[str, Any],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Single OpenRouter chat-completion call with json_schema → json_object fallback.

    Returns a dict mirroring the harness's transport result shape:
    `{result_text, tokens_in, tokens_out, cost_usd, latency_ms, error,
    error_detail, rate_limit_retries, fallback_to_json_object}`.

    The strict→json_object retry covers Gemini's well-known JSON Schema
    rejection (it doesn't accept `$schema` / `$id` / `additionalProperties: false` /
    `anyOf`); the schema is already inlined in the prompt body so the
    prompt + downstream json-repair handle semantic enforcement on the
    fallback path.

    429s are absorbed by `_post_with_rate_limit_handling` with a
    dedicated 6-attempt schedule (15-120s backoff or `Retry-After`
    header). Rate-limit retries don't count against the producer's
    `--retry-on-error` budget — they're a transient-window concern,
    not a substantive failure.

    Cost is read from `usage.cost` (USD) when OpenRouter populates it
    (we request via `usage: {include: true}` in the body); otherwise
    falls back to per-token estimate using the conservative module
    constants. OpenRouter's value is authoritative — Gemini's hidden
    reasoning tokens / cache fees aren't in `prompt_tokens` /
    `completion_tokens`, so the estimate undercounts ~5x.
    """
    use_strict = True
    body = _build_request_body(
        model=model,
        full_prompt=full_prompt,
        schema=schema,
        max_tokens=max_tokens,
        use_strict_schema=use_strict,
    )
    t0 = time.time()
    last_status: int | None = None
    last_text: str | None = None
    fallback_used = False
    rate_limit_retries = 0
    try:
        resp, rl_used, _ = _post_with_rate_limit_handling(
            client,
            openrouter_url=openrouter_url,
            api_key=api_key,
            body=body,
        )
        rate_limit_retries += rl_used
    except httpx.HTTPError as exc:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": int((time.time() - t0) * 1000),
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    if resp is None:
        # Rate-limit budget exhausted — surface as a transport error.
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": int((time.time() - t0) * 1000),
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "rate_limit",
            "error_detail": (
                f"rate-limit budget exhausted after {rate_limit_retries} retries"
            ),
        }

    # Fall back to json_object on ANY 400 in strict mode. OpenRouter
    # exposes a wide mix of upstream providers, each with its own JSON
    # Schema validator quirks: Gemini's `additionalProperties` /
    # `unspecified-property` errors, OpenAI's `additionalProperties: false`
    # requirement, Cerebras's outright "json_schema not supported", etc.
    # Matching the verbose error vocabulary one-by-one is fragile (we
    # already missed "schema at top-level requires unspecified property"
    # in v0.1.0); the schema is already inlined in the prompt body so
    # the json_object fallback path enforces structure via the
    # downstream parse_and_validate. This mirrors the pilot harness's
    # rule (`use_strict_schema and is_bad_request`).
    if resp.status_code == 400 and use_strict:
        last_status = resp.status_code
        last_text = resp.text
        fallback_used = True
        body = _build_request_body(
            model=model,
            full_prompt=full_prompt,
            schema=schema,
            max_tokens=max_tokens,
            use_strict_schema=False,
        )
        try:
            resp, rl_used, _ = _post_with_rate_limit_handling(
                client,
                openrouter_url=openrouter_url,
                api_key=api_key,
                body=body,
            )
            rate_limit_retries += rl_used
        except httpx.HTTPError as exc:
            return {
                "result_text": None,
                "tokens_in": None,
                "tokens_out": None,
                "cost_usd": None,
                "latency_ms": int((time.time() - t0) * 1000),
                "rate_limit_retries": rate_limit_retries,
                "fallback_to_json_object": True,
                "error": "transport",
                "error_detail": f"fallback {type(exc).__name__}: {exc}",
            }
        if resp is None:
            return {
                "result_text": None,
                "tokens_in": None,
                "tokens_out": None,
                "cost_usd": None,
                "latency_ms": int((time.time() - t0) * 1000),
                "rate_limit_retries": rate_limit_retries,
                "fallback_to_json_object": True,
                "error": "rate_limit",
                "error_detail": "rate-limit budget exhausted on fallback",
            }

    latency_ms = int((time.time() - t0) * 1000)

    if resp.status_code != 200:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": fallback_used,
            "error": "transport",
            "error_detail": f"http {resp.status_code}: {resp.text[:500]}",
        }

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": fallback_used,
            "error": "transport",
            "error_detail": f"response not json: {exc}",
        }

    choices = payload.get("choices") or []
    if not choices:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": fallback_used,
            "error": "transport",
            "error_detail": (
                f"empty choices; last_status={last_status} last_text={last_text!r}"
            ),
        }
    message = (choices[0] or {}).get("message") or {}
    result_text = message.get("content")
    usage = payload.get("usage") or {}
    cost_usd = usage.get("cost")
    if cost_usd is None:
        # Fallback estimate when OpenRouter doesn't return cost in the
        # response. Conservative; documented to undercount when hidden
        # reasoning tokens are involved.
        cost_usd = estimate_cost_usd(
            usage.get("prompt_tokens"), usage.get("completion_tokens")
        )
    return {
        "result_text": result_text,
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "cost_usd": float(cost_usd) if cost_usd is not None else None,
        "rate_limit_retries": rate_limit_retries,
        "fallback_to_json_object": fallback_used,
        "latency_ms": latency_ms,
        "error": None,
        "error_detail": None,
    }


# ---------------------------------------------------------------------------
# Google AI Studio adapter (native Gemini API)
# ---------------------------------------------------------------------------


def _google_post_with_rate_limit_handling(
    client: httpx.Client,
    *,
    url: str,
    body: dict[str, Any],
    rate_limit_retries: int = RATE_LIMIT_MAX_RETRIES,
) -> tuple[httpx.Response | None, int, str | None]:
    """Google's native API uses 429s differently than OpenRouter — the
    rate-limit window is per-project per-minute, and the Retry-After
    header is sometimes absent. Same backoff schedule as OpenRouter.
    """
    last_resp: httpx.Response | None = None
    last_detail: str | None = None
    for attempt in range(rate_limit_retries + 1):
        resp = client.post(url, json=body)
        if resp.status_code != 429:
            return resp, attempt, None
        last_resp = resp
        last_detail = resp.text[:200]
        if attempt >= rate_limit_retries:
            break
        retry_after = _parse_retry_after(resp.headers, resp.status_code)
        if retry_after is None:
            schedule_idx = min(attempt, len(RATE_LIMIT_BACKOFF_SCHEDULE) - 1)
            sleep_for = RATE_LIMIT_BACKOFF_SCHEDULE[schedule_idx]
        else:
            sleep_for = retry_after
        time.sleep(sleep_for)
    return last_resp, rate_limit_retries, last_detail


def _resolve_google_urls(base_url: str, model: str, *, api_key: str) -> tuple[str, str]:
    """Return `(chat_url, models_list_url)` from a configured URL.

    Accepts two forms:
    - **Base URL** (`https://generativelanguage.googleapis.com/v1beta`):
      we append `/models/{model}:generateContent?key=...` for chat and
      `/models?key=...` for the healthcheck.
    - **Full chat URL** (`https://.../v1beta/models/<m>:generateContent`):
      we use it as-is for chat (the `model` parameter is ignored — the
      URL already pins the model) and walk back to the base for the
      healthcheck endpoint.

    The full-URL form is what most operators put in `.env` because it's
    what Google's docs / Cloud Console show; the base-URL form matches
    our `DEFAULT_GOOGLE_AI_STUDIO_URL` constant.
    """
    url = base_url.rstrip("/")
    if ":generateContent" in url:
        chat_url = f"{url}?key={api_key}"
        # Strip the `/models/<model>:generateContent` tail to recover
        # the base for the healthcheck. `rsplit` handles arbitrary
        # path prefixes (`/v1`, `/v1beta`, custom proxies, etc.).
        base = url.rsplit("/models/", 1)[0]
        models_url = f"{base}/models?key={api_key}"
    else:
        chat_url = f"{url}/models/{model}:generateContent?key={api_key}"
        models_url = f"{url}/models?key={api_key}"
    return chat_url, models_url


def call_google_ai_studio(
    *,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    full_prompt: str,
    schema: dict[str, Any],  # included for signature parity with OpenRouter
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Single Google AI Studio (native Gemini API) call.

    Mirrors `call_openrouter`'s return shape so the dispatcher in
    `call_llm` doesn't need to know which provider was used:
    `{result_text, tokens_in, tokens_out, cost_usd, latency_ms,
    rate_limit_retries, fallback_to_json_object, error, error_detail}`.

    Endpoint resolution: see `_resolve_google_urls` — accepts both base
    URL and full-endpoint forms.
    Auth: query-string `?key=` (NOT bearer header).
    Schema enforcement: `responseMimeType: "application/json"` is the
    cheapest correct path — the prompt body already inlines the JSON
    Schema and our downstream `parse_and_validate` + json-repair handle
    edge cases. Native `responseSchema` would require translating each
    prompt's schema to Google's restricted subset (rejects `$schema`,
    `additionalProperties`, `anyOf`, `oneOf`); not worth it.

    Cost is computed locally from `usageMetadata.promptTokenCount` /
    `candidatesTokenCount` since Google's response doesn't carry
    `usage.cost` like OpenRouter does. Pricing constants live near the
    top of this module; tune after observing actual Google Cloud
    invoices.
    """
    url, _ = _resolve_google_urls(base_url, model, api_key=api_key)
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    t0 = time.time()
    rate_limit_retries = 0
    try:
        resp, rl_used, _ = _google_post_with_rate_limit_handling(
            client, url=url, body=body
        )
        rate_limit_retries += rl_used
    except httpx.HTTPError as exc:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": int((time.time() - t0) * 1000),
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"{type(exc).__name__}: {exc}",
        }

    if resp is None:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": int((time.time() - t0) * 1000),
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "rate_limit",
            "error_detail": (
                f"rate-limit budget exhausted after {rate_limit_retries} retries"
            ),
        }

    latency_ms = int((time.time() - t0) * 1000)

    if resp.status_code != 200:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"http {resp.status_code}: {resp.text[:500]}",
        }

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"response not json: {exc}",
        }

    # Google may return `promptFeedback.blockReason` when safety filters
    # trip — surface as a transport error so the retry loop can decide.
    block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"blocked by safety: {block_reason}",
        }

    candidates = payload.get("candidates") or []
    if not candidates:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "rate_limit_retries": rate_limit_retries,
            "fallback_to_json_object": False,
            "error": "transport",
            "error_detail": f"empty candidates; payload head: {str(payload)[:300]}",
        }

    # `content.parts[]` is an array — usually one text part, but some
    # responses split into multiple parts; concatenate.
    parts = (candidates[0] or {}).get("content", {}).get("parts") or []
    text_parts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    result_text = "".join(text_parts)

    usage = payload.get("usageMetadata") or {}
    tokens_in = usage.get("promptTokenCount")
    tokens_out = usage.get("candidatesTokenCount")
    # Google charges thinking tokens at the same rate as output for
    # Gemini Flash-Lite when reasoning is enabled. We add them to
    # `tokens_out` for cost purposes — invisible from the response
    # shape but accounted for in the budget cap.
    thinking = usage.get("thoughtsTokenCount") or 0
    tokens_out_for_cost = (tokens_out or 0) + thinking
    cost_usd = estimate_cost_usd(
        tokens_in, tokens_out_for_cost, provider=PROVIDER_GOOGLE
    )
    return {
        "result_text": result_text,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "rate_limit_retries": rate_limit_retries,
        "fallback_to_json_object": False,  # Google doesn't have this concept
        "latency_ms": latency_ms,
        "error": None,
        "error_detail": None,
    }


def call_llm(
    *,
    provider: str,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    model: str,
    full_prompt: str,
    schema: dict[str, Any],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Provider dispatcher. Routes to OpenRouter or Google AI Studio
    based on `provider`; returns the same standardised result dict from
    either path. Validates `provider` against `SUPPORTED_PROVIDERS`.
    """
    if provider == PROVIDER_OPENROUTER:
        return call_openrouter(
            client=client,
            openrouter_url=base_url,
            api_key=api_key,
            model=model,
            full_prompt=full_prompt,
            schema=schema,
            max_tokens=max_tokens,
        )
    if provider == PROVIDER_GOOGLE:
        return call_google_ai_studio(
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            full_prompt=full_prompt,
            schema=schema,
            max_tokens=max_tokens,
        )
    raise ValueError(
        f"unsupported provider: {provider!r} (expected one of {SUPPORTED_PROVIDERS})"
    )


def estimate_cost_usd(
    tokens_in: int | None,
    tokens_out: int | None,
    *,
    provider: str = PROVIDER_OPENROUTER,
) -> float:
    """Estimated USD cost for one call given the per-token Flash-Lite rates.

    Conservative: defaults to `0.0` when either token count is missing
    (the budget cap stays generous when token data is partial). Rates
    differ per provider — OpenRouter has a margin over the upstream
    Google price; Google direct is cheaper.
    """
    if not tokens_in or not tokens_out:
        return 0.0
    if provider == PROVIDER_GOOGLE:
        return (
            tokens_in * GOOGLE_FLASH_LITE_INPUT_RATE_USD
            + tokens_out * GOOGLE_FLASH_LITE_OUTPUT_RATE_USD
        )
    return (
        tokens_in * OPENROUTER_FLASH_LITE_INPUT_RATE_USD
        + tokens_out * OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD
    )


# ---------------------------------------------------------------------------
# Per-prompt call wrapper with retry + parse
# ---------------------------------------------------------------------------


def _retry_backoff_seconds(attempt: int) -> float:
    return float(min(2 ** (attempt - 1), 8))


# Heuristic threshold: if the previous attempt's output token count is
# at or above this fraction of `max_tokens`, the response likely hit
# the budget and got truncated mid-output. Google's API counts reasoning
# tokens toward `completion_tokens` but NOT toward the `maxOutputTokens`
# cap, so a truncated response sometimes overshoots the cap by a small
# margin (e.g. 16778 against 16384). 0.95 catches those without
# triggering on legitimate near-cap responses (the corpus distribution
# has p99=2979 against 32768; legitimate responses don't get within
# 95% of the cap at any quantile).
_TRUNCATION_TOKEN_RATIO = 0.95

# Suffix appended to the user prompt on a retry whose previous attempt
# looked like a runaway-output truncation. Closes the model-degenerate
# state where short heavy-rhetoric speeches (104 words of personal
# attack with sarcasm + multiple political accusations) trip Gemini
# into emitting one DQI marker per phrase indefinitely. The terseness
# anchor breaks the runaway by capping marker count and rationale
# length explicitly. Romanian + English mixed wording matches the
# style of the rest of the production prompts.
_TERSENESS_RETRY_SUFFIX = """

---

# CRITICAL — token-budget guard for this retry

The previous attempt produced runaway output that exhausted the token cap before completing all required schema fields (typically `rationale` was missing). On THIS attempt:

- Emit AT MOST 6 markers across the entire response.
- The `rationale` field MUST be 4 sentences or fewer.
- Do NOT enumerate every phrase as a separate marker. Pick the ONE most diagnostic anchor for each dimension and stop.
- Every required field MUST be present and complete in the JSON output before the response ends.

Be concise. Brevity is correctness here.
"""


def _is_truncation_failure(call: "CallResult", max_tokens: int) -> bool:
    """Heuristic: the previous attempt looks like it ran out of output
    tokens before finishing the JSON.

    Two conjunct signals:
      (a) `error == "schema_invalid"` AND the message mentions a
          missing required property — typically `'rationale' is a
          required property` since schemas list rationale near the end.
      (b) `tokens_out >= 95% of max_tokens` — the strongest signal that
          the response hit the budget. Used as the gate so legitimate
          schema-invalid failures (e.g. wrong enum values that survive
          the salvage) don't trigger an unnecessary terseness retry.
    """
    if call.error != "schema_invalid":
        return False
    msg = call.error_detail or ""
    if "is a required property" not in msg:
        return False
    out = call.tokens_out or 0
    return out >= int(max_tokens * _TRUNCATION_TOKEN_RATIO)


def _run_one_prompt(
    *,
    client: httpx.Client,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt_kind: str,
    full_prompt: str,
    schema: dict[str, Any],
    speech_text: str,
    max_tokens: int,
    max_retries: int,
) -> CallResult:
    """One prompt call + parse + validate + offset recovery, with retry budget.

    Configuration errors (missing api_key, missing prompt) short-circuit
    and never retry; transport / json_parse / schema_invalid are
    retried until the budget is exhausted.

    Terseness-on-retry: when the previous attempt's failure looks like
    a runaway-output truncation (`_is_truncation_failure`), the retry's
    user prompt gets a terseness suffix appended. Re-uses the same
    retry budget — no extra HTTP call beyond what `--retry-on-error`
    already authorises.
    """
    last: CallResult | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(_retry_backoff_seconds(attempt))
        # Inject terseness suffix on retry when the previous attempt
        # truncated. The first attempt always uses the unmodified
        # prompt, so non-truncation calls pay zero cost.
        effective_prompt = full_prompt
        if (
            attempt > 0
            and last is not None
            and _is_truncation_failure(last, max_tokens)
        ):
            effective_prompt = full_prompt + _TERSENESS_RETRY_SUFFIX
        transport = call_llm(
            provider=provider,
            client=client,
            base_url=base_url,
            api_key=api_key,
            model=model,
            full_prompt=effective_prompt,
            schema=schema,
            max_tokens=max_tokens,
        )
        raw_text = transport.get("result_text")
        if transport.get("error"):
            last = CallResult(
                prompt_kind=prompt_kind,
                output=None,
                error=transport["error"],
                error_detail=transport.get("error_detail"),
                fragments_not_found=[],
                parse_repaired=False,
                tokens_in=transport.get("tokens_in"),
                tokens_out=transport.get("tokens_out"),
                cost_usd=transport.get("cost_usd"),
                latency_ms=transport.get("latency_ms", 0),
                rate_limit_retries=transport.get("rate_limit_retries", 0),
                fallback_to_json_object=transport.get("fallback_to_json_object", False),
            )
        else:
            output, err_class, err_detail, repaired = parse_and_validate(
                raw_text or "", schema
            )
            fragments = _collect_fragments_not_found(output, speech_text)
            last = CallResult(
                prompt_kind=prompt_kind,
                output=output,
                error=err_class,
                error_detail=err_detail,
                fragments_not_found=fragments,
                parse_repaired=repaired,
                tokens_in=transport.get("tokens_in"),
                tokens_out=transport.get("tokens_out"),
                cost_usd=transport.get("cost_usd"),
                latency_ms=transport.get("latency_ms", 0),
                rate_limit_retries=transport.get("rate_limit_retries", 0),
                fallback_to_json_object=transport.get("fallback_to_json_object", False),
            )
        if not last.error:
            return last
    return last  # type: ignore[return-value]  # loop body always sets `last`


# ---------------------------------------------------------------------------
# Per-speech pipeline
# ---------------------------------------------------------------------------


def _run_pipeline_for_speech(
    *,
    client: httpx.Client,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    speech_text: str,
    prompts: dict[str, tuple[str, dict[str, Any]]],
    max_tokens: int,
    max_retries: int,
) -> tuple[dict[str, dict[str, Any] | None], dict[str, Any], list[str]]:
    """Run hawkins → voice (conditional) → dqi → vparty for one speech.

    Returns `(per_prompt_outputs, totals, errors)` where `totals` carries
    aggregate `{tokens_in, tokens_out, cost_usd, rate_limit_retries,
    fallback_count, repaired_count}` — the cost is the sum of per-call
    OpenRouter `usage.cost` values when available, falling back to the
    per-token estimate when missing. The diagnostic counters surface
    silent-recovery events (429s absorbed by rate-limit handling, 400s
    recovered by json_object fallback, json-repair fallbacks) so the
    operator has post-run visibility.
    """
    results: dict[str, dict[str, Any] | None] = {
        "hawkins": None,
        "voice": None,
        "dqi": None,
        "vparty": None,
    }
    totals = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "rate_limit_retries": 0,
        "fallback_count": 0,
        "repaired_count": 0,
        "call_count": 0,
    }
    errors: list[str] = []

    def _accumulate(call: CallResult) -> None:
        totals["call_count"] += 1
        if call.tokens_in:
            totals["tokens_in"] += call.tokens_in
        if call.tokens_out:
            totals["tokens_out"] += call.tokens_out
        # Prefer OpenRouter's authoritative cost; fall back to estimate
        # when the response didn't carry one (older endpoints, non-OR
        # backends).
        if call.cost_usd is not None:
            totals["cost_usd"] += call.cost_usd
        else:
            totals["cost_usd"] += estimate_cost_usd(call.tokens_in, call.tokens_out)
        totals["rate_limit_retries"] += call.rate_limit_retries
        if call.fallback_to_json_object:
            totals["fallback_count"] += 1
        if call.parse_repaired:
            totals["repaired_count"] += 1
        if call.error:
            errors.append(f"{call.prompt_kind}: {call.error} ({call.error_detail})")

    # 1. Hawkins
    hawkins_text, hawkins_schema = prompts["hawkins"]
    hawkins_call = _run_one_prompt(
        client=client,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt_kind="hawkins",
        full_prompt=build_prompt_for_speech(hawkins_text, speech_text, hawkins_schema),
        schema=hawkins_schema,
        speech_text=speech_text,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
    _accumulate(hawkins_call)
    # Only consider the output usable when the call succeeded — otherwise
    # we'd store malformed shapes (e.g., the model occasionally emits
    # `markers` as a string instead of an array; schema validation rejects
    # it, salvage can't fix it because there's no list to filter, and a
    # downstream `for m in markers` would iterate the string character-
    # by-character → `AttributeError: 'str' object has no attribute 'get'`
    # at `build_prompt_for_voice`'s `m.get("evidence")` call). The Hawkins
    # / DQI / V-Party blocks below all share this gate; only validated
    # outputs cross into the per-speech results dict.
    if hawkins_call.output and not hawkins_call.error:
        results["hawkins"] = hawkins_call.output

    # 2. Voice (conditional on Hawkins markers)
    hawkins_markers_raw = (results["hawkins"] or {}).get("markers")
    # Defence-in-depth: even with the validation gate above, refuse to
    # iterate anything that isn't a list (post-mortem invariant).
    hawkins_markers = (
        hawkins_markers_raw if isinstance(hawkins_markers_raw, list) else []
    )
    if hawkins_markers:
        voice_text, voice_schema = prompts["voice"]
        voice_prompt = build_prompt_for_voice(
            voice_text, speech_text, hawkins_markers, voice_schema
        )
        if voice_prompt:
            voice_call = _run_one_prompt(
                client=client,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                prompt_kind="voice",
                full_prompt=voice_prompt,
                schema=voice_schema,
                speech_text=speech_text,
                max_tokens=max_tokens,
                max_retries=max_retries,
            )
            _accumulate(voice_call)
            if voice_call.output and not voice_call.error:
                results["voice"] = voice_call.output

    # 3. DQI (independent)
    dqi_text, dqi_schema = prompts["dqi"]
    dqi_call = _run_one_prompt(
        client=client,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt_kind="dqi",
        full_prompt=build_prompt_for_speech(dqi_text, speech_text, dqi_schema),
        schema=dqi_schema,
        speech_text=speech_text,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
    _accumulate(dqi_call)
    if dqi_call.output and not dqi_call.error:
        results["dqi"] = dqi_call.output

    # 4. V-Party (independent)
    vparty_text, vparty_schema = prompts["vparty"]
    vparty_call = _run_one_prompt(
        client=client,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt_kind="vparty",
        full_prompt=build_prompt_for_speech(vparty_text, speech_text, vparty_schema),
        schema=vparty_schema,
        speech_text=speech_text,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
    _accumulate(vparty_call)
    if vparty_call.output and not vparty_call.error:
        results["vparty"] = vparty_call.output

    return results, totals, errors


# ---------------------------------------------------------------------------
# Existing-entries reuse + write
# ---------------------------------------------------------------------------


def _existing_entries(path: Path) -> dict[str, dict[str, Any]]:
    """Read the prior discourse file (if any). Missing / unreadable → empty."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {rid: entry for rid, entry in data.items() if isinstance(entry, dict)}


def _entry_is_clean(entry: dict[str, Any]) -> bool:
    """An entry is clean iff Hawkins / DQI / V-Party all have payloads
    AND the entry's `_meta.errors[]` is absent. Voice may legitimately
    be null when Hawkins emitted zero markers — checked by deferring
    to Hawkins's marker count.

    The clean-test is what gates fingerprint-skip on re-run: dirty
    entries (missing payloads or carrying errors) are NOT reused, so
    the next run picks them up automatically. Without this, a 429-
    burst that wrote partial entries would block retries forever.
    """
    meta = entry.get("_meta") or {}
    if isinstance(meta, dict) and meta.get("errors"):
        return False
    hawkins = entry.get("hawkins")
    dqi = entry.get("dqi")
    vparty = entry.get("vparty")
    if hawkins is None or dqi is None or vparty is None:
        return False
    voice = entry.get("voice")
    if voice is None:
        # Legitimate when Hawkins emitted no markers.
        markers = (hawkins or {}).get("markers")
        if markers:
            return False
    return True


def _build_entry(
    *,
    fingerprint: str,
    sidecar_content_sha: str,
    model: str,
    results: dict[str, dict[str, Any] | None],
    errors: list[str],
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "namespace": DISCOURSE_NAMESPACE,
        "producer": DISCOURSE_PRODUCER,
        "version": DISCOURSE_VERSION_DOTTED,
        "model_id": model,
        "source_sidecar_content_sha": sidecar_content_sha,
        "indexed_at": _now_iso(),
        "prompt_versions": dict(PROMPT_VERSIONS),
    }
    if errors:
        meta["errors"] = errors
    return {
        "_meta": meta,
        "text_fingerprint": fingerprint,
        "hawkins": results.get("hawkins"),
        "voice": results.get("voice"),
        "dqi": results.get("dqi"),
        "vparty": results.get("vparty"),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def analyze_sidecar(
    sidecar_path: Path,
    *,
    provider: str = DEFAULT_PROVIDER,
    openrouter_url: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    force: bool = False,
    write: bool = True,
    dry_run: bool = False,
    retry_on_error: int = 1,
    max_words: int = DEFAULT_MAX_WORDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    budget_remaining_usd: float | None = None,
    client: httpx.Client | None = None,
    prompts: dict[str, tuple[str, dict[str, Any]]] | None = None,
    log: JsonlLogger | None = None,
) -> AnalyzeResult:
    """Analyse every substantive speech in one sidecar.

    Idempotent: when a prior discourse file carries an entry whose
    `text_fingerprint` matches the current speech's normalised text, the
    prior payload is reused verbatim and no API call is made for that
    record. Set `force=True` to re-analyze every record.

    `dry_run=True` walks the sidecar + reports counts without any HTTP
    call or file write. `write=False` skips the disk write but still
    issues HTTP calls — useful when callers want to inspect results
    without persisting (rare; not exposed via CLI).

    `budget_remaining_usd` is the soft cap. The producer estimates
    cumulative spend per call from `tokens_in × INPUT_RATE +
    tokens_out × OUTPUT_RATE` (Flash-Lite rates as module constants). If
    the cap is hit mid-sidecar, the in-flight record's results are
    persisted (atomic-rename contract), the rest of the records are NOT
    coded (their entries stay null), and `action="budget-exhausted"`.

    `client` is an optional `httpx.Client` for connection reuse across
    many sidecars; the CLI's batch path passes one. When None, the
    function builds a short-lived client per call.

    `prompts` is an override hook for tests / non-default layouts —
    a `{kind: (prompt_text, schema_dict)}` dict. When None, prompts are
    loaded from `<repo-root>/prompts/`.
    """
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id="",
            action="error",
            errors=[f"read sidecar: {exc}"],
        )
    document_id = sidecar.get("document_id") or ""
    if not document_id:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id="",
            action="error",
            errors=["sidecar missing document_id"],
        )
    sidecar_content_sha = sidecar.get("content_sha") or ""

    basename = _resolve_basename(sidecar_path)
    target = sidecar_path.parent / discourse_filename(basename)

    tasks, long_skipped = _iter_speech_tasks(sidecar, max_words=max_words)
    if not tasks:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="skipped",
            skipped=long_skipped,
            file_path=target if target.exists() else None,
        )

    prior = {} if force else _existing_entries(target)

    new_entries: dict[str, dict[str, Any]] = {}
    reused: dict[str, dict[str, Any]] = {}
    needs_call: list[_SpeechTask] = []

    for task in tasks:
        prior_entry = prior.get(task.record_id)
        if (
            not force
            and isinstance(prior_entry, dict)
            and prior_entry.get("text_fingerprint") == task.fingerprint
            and _entry_is_clean(prior_entry)
        ):
            reused[task.record_id] = prior_entry
            continue
        needs_call.append(task)

    if dry_run:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="dry-run",
            analyzed=len(needs_call),
            reused=len(reused),
            skipped=long_skipped,
            file_path=target,
        )

    if not needs_call:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="skipped",
            analyzed=0,
            reused=len(reused),
            skipped=long_skipped,
            file_path=target,
        )

    if provider not in SUPPORTED_PROVIDERS:
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="error",
            errors=[f"unsupported provider: {provider!r}"],
            file_path=target,
        )

    if api_key is None:
        key_var = (
            "OPENROUTER_API_KEY"
            if provider == PROVIDER_OPENROUTER
            else "GOOGLE_AI_STUDIO_API_KEY"
        )
        return AnalyzeResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="error",
            errors=[f"{key_var} not set"],
            file_path=target,
        )

    # Resolve base URL: explicit `base_url` wins, then provider-specific
    # legacy `openrouter_url`, then provider default.
    if base_url is not None:
        url = base_url.rstrip("/")
    elif provider == PROVIDER_OPENROUTER and openrouter_url is not None:
        url = openrouter_url.rstrip("/")
    elif provider == PROVIDER_OPENROUTER:
        url = DEFAULT_OPENROUTER_URL
    else:  # provider == PROVIDER_GOOGLE
        url = DEFAULT_GOOGLE_AI_STUDIO_URL

    # Resolve model: explicit wins, otherwise per-provider default.
    resolved_model = model or DEFAULT_MODEL_BY_PROVIDER[provider]

    prompt_bundle = prompts if prompts is not None else _load_prompts()

    owns_client = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(300.0))
        owns_client = True

    cumulative_cost = 0.0
    budget_exhausted = False
    rate_limit_retries_total = 0
    fallback_count_total = 0
    repaired_count_total = 0
    call_count_total = 0
    tokens_in_total = 0
    tokens_out_total = 0
    hawkins_with_markers = 0
    hawkins_no_markers = 0
    failed_records: list[str] = []
    aggregated_errors: list[str] = []
    t_start = time.monotonic()

    try:
        for task in needs_call:
            results, totals, per_speech_errors = _run_pipeline_for_speech(
                client=client,
                provider=provider,
                base_url=url,
                api_key=api_key,
                model=resolved_model,
                speech_text=task.text,
                prompts=prompt_bundle,
                max_tokens=max_tokens,
                max_retries=retry_on_error,
            )
            cumulative_cost += totals["cost_usd"]
            rate_limit_retries_total += totals.get("rate_limit_retries", 0)
            fallback_count_total += totals.get("fallback_count", 0)
            repaired_count_total += totals.get("repaired_count", 0)
            call_count_total += totals.get("call_count", 0)
            tokens_in_total += totals.get("tokens_in", 0)
            tokens_out_total += totals.get("tokens_out", 0)
            # Hawkins-marker distribution surfaces how often the voice
            # pass actually fires. Useful sanity check on prompt
            # calibration: if 100% of speeches go to "no markers",
            # something is wrong with Hawkins.
            if isinstance(results.get("hawkins"), dict):
                if (results["hawkins"] or {}).get("markers"):
                    hawkins_with_markers += 1
                else:
                    hawkins_no_markers += 1
            entry = _build_entry(
                fingerprint=task.fingerprint,
                sidecar_content_sha=sidecar_content_sha,
                model=resolved_model,
                results=results,
                errors=per_speech_errors,
            )
            # Persist ONLY clean entries (all required frameworks
            # populated, no errors). Failed records are dropped from
            # this run — they're absent from the output file, so the
            # next run won't see a fingerprint match and will retry
            # cleanly. This is the "save what succeeded, retry the
            # rest" contract the operator asked for.
            entry_clean = _entry_is_clean(entry)
            if entry_clean:
                new_entries[task.record_id] = entry
                # Periodic atomic flush so a Ctrl+C between records
                # doesn't lose the in-memory work. Cost: one extra
                # .part-rename per PARTIAL_FLUSH_EVERY records.
                if write and (len(new_entries) % PARTIAL_FLUSH_EVERY) == 0:
                    out_partial: dict[str, dict[str, Any]] = {}
                    out_partial.update(reused)
                    out_partial.update(new_entries)
                    _atomic_write_json(target, out_partial)
            else:
                failed_records.append(task.record_id)
                if per_speech_errors:
                    aggregated_errors.extend(per_speech_errors)
            # Per-speech telemetry — one JSONL line per coded record.
            # Logs the headline scores + diagnostic counters so the
            # operator can `tail -f` the log file and see live activity.
            if log is not None:
                hk = results.get("hawkins") or {}
                vp = results.get("vparty") or {}
                dq = results.get("dqi") or {}
                vc = results.get("voice")
                log.write(
                    {
                        "ts": _now_iso(),
                        "sidecar": sidecar_path.name,
                        "record_id": task.record_id,
                        "outcome": "ok" if entry_clean else "failed",
                        "hawkins_score": hk.get("score"),
                        "hawkins_markers": len(hk.get("markers") or [])
                        if isinstance(hk, dict)
                        else 0,
                        "vparty_score": vp.get("score"),
                        "vparty_markers": len(vp.get("markers") or [])
                        if isinstance(vp, dict)
                        else 0,
                        "dqi_level": dq.get("level_of_justification"),
                        "voice_ran": vc is not None,
                        "tokens_in": totals.get("tokens_in", 0),
                        "tokens_out": totals.get("tokens_out", 0),
                        "cost_usd": round(totals.get("cost_usd", 0.0), 6),
                        "calls": totals.get("call_count", 0),
                        "fallbacks": totals.get("fallback_count", 0),
                        "rate_limit_retries": totals.get("rate_limit_retries", 0),
                        "repaired": totals.get("repaired_count", 0),
                        "errors": per_speech_errors,
                    }
                )
            if (
                budget_remaining_usd is not None
                and cumulative_cost >= budget_remaining_usd
            ):
                budget_exhausted = True
                break
    except BaseException:
        # Includes KeyboardInterrupt + GeneratorExit + SystemExit.
        # Flush whatever we have so a Ctrl+C between flush windows
        # doesn't lose the partially-coded sidecar — re-raise after the
        # write so the outer cmd_analyze handler still sees the signal.
        if write and new_entries:
            try:
                out_partial = {**reused, **new_entries}
                _atomic_write_json(target, out_partial)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        if owns_client:
            client.close()
    elapsed_s = time.monotonic() - t_start

    # Output dict is reused (clean prior entries that matched fingerprint)
    # ∪ newly clean entries from this run. Dirty/missing-prior records
    # NOT in this set survive their next run because no fingerprint
    # match exists in the file.
    out: dict[str, dict[str, Any]] = {}
    out.update(reused)
    out.update(new_entries)

    # Write iff this run produced at least one new clean entry. Every-
    # record-failed runs leave the prior file (if any) untouched —
    # partial-corruption protection. Re-running picks up the failed
    # records cleanly because no fingerprint match exists for them.
    if write and new_entries:
        _atomic_write_json(target, out)

    if not new_entries and failed_records:
        # Every record we tried this run failed. Prior clean entries
        # (if any) survive in the file untouched.
        action = "all-failed"
    elif budget_exhausted:
        action = "budget-exhausted"
    else:
        action = "analyzed"

    return AnalyzeResult(
        sidecar_path=sidecar_path,
        document_id=document_id,
        action=action,
        analyzed=len(new_entries),
        reused=len(reused),
        skipped=long_skipped,
        failed=len(failed_records),
        file_path=target,
        cost_estimate_usd=cumulative_cost,
        rate_limit_retries=rate_limit_retries_total,
        fallback_count=fallback_count_total,
        repaired_count=repaired_count_total,
        call_count=call_count_total,
        tokens_in_total=tokens_in_total,
        tokens_out_total=tokens_out_total,
        hawkins_with_markers=hawkins_with_markers,
        hawkins_no_markers=hawkins_no_markers,
        elapsed_s=elapsed_s,
        errors=aggregated_errors[:10],  # cap to keep AnalyzeResult small
    )


def analyze_all(
    sidecar_paths: Iterable[Path],
    *,
    provider: str = DEFAULT_PROVIDER,
    openrouter_url: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    force: bool = False,
    write: bool = True,
    dry_run: bool = False,
    retry_on_error: int = 1,
    max_words: int = DEFAULT_MAX_WORDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    budget_usd: float | None = None,
    client: httpx.Client | None = None,
    prompts: dict[str, tuple[str, dict[str, Any]]] | None = None,
    log: JsonlLogger | None = None,
) -> Iterator[AnalyzeResult]:
    """Iterate `analyze_sidecar(...)` over many sidecars sequentially.

    The HTTP client is reused across calls when supplied. If `budget_usd`
    is set, the cumulative cost across all sidecars is tracked; once
    exceeded, subsequent calls receive `budget_remaining_usd <= 0` and
    return `action="budget-exhausted"` without making any API calls.
    """
    owns_client = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(300.0))
        owns_client = True

    cumulative_cost = 0.0
    try:
        for path in sidecar_paths:
            remaining: float | None
            if budget_usd is not None:
                remaining = max(0.0, budget_usd - cumulative_cost)
                if remaining <= 0:
                    yield AnalyzeResult(
                        sidecar_path=path,
                        document_id="",
                        action="budget-exhausted",
                    )
                    continue
            else:
                remaining = None
            result = analyze_sidecar(
                path,
                provider=provider,
                openrouter_url=openrouter_url,
                base_url=base_url,
                api_key=api_key,
                model=model,
                force=force,
                write=write,
                dry_run=dry_run,
                retry_on_error=retry_on_error,
                max_words=max_words,
                max_tokens=max_tokens,
                budget_remaining_usd=remaining,
                client=client,
                prompts=prompts,
                log=log,
            )
            cumulative_cost += result.cost_estimate_usd
            yield result
            if result.action == "budget-exhausted":
                # Subsequent paths short-circuit to "budget-exhausted"
                # via the remaining-cap check above.
                budget_usd = cumulative_cost
    finally:
        if owns_client:
            client.close()


def healthcheck(
    openrouter_url: str | None = None,
    *,
    api_key: str,
    provider: str = PROVIDER_OPENROUTER,
    base_url: str | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """One-shot reachability + auth probe for the chosen provider.

    OpenRouter: `GET /models` with `Authorization: Bearer <key>`,
    response carries `data: [...]`.
    Google: `GET /models?key=<key>`, response carries `models: [...]`.

    `openrouter_url` is the legacy positional parameter kept for
    backward compatibility with tests; new callers pass `base_url`.
    Returns `(ok, detail)` where detail is the model count on success
    or a short error message on failure.
    """
    if provider == PROVIDER_OPENROUTER:
        url = (base_url or openrouter_url or DEFAULT_OPENROUTER_URL).rstrip("/")
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(
                    url + "/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code != 200:
                    return False, f"http {r.status_code}: {r.text[:200]}"
                payload = r.json()
                data = payload.get("data") or []
                return True, f"{len(data)} models"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    if provider == PROVIDER_GOOGLE:
        url = (base_url or DEFAULT_GOOGLE_AI_STUDIO_URL).rstrip("/")
        # Use the resolver so a full `:generateContent` URL still lets
        # us walk back to a valid `/models?key=…` healthcheck endpoint.
        _, models_url = _resolve_google_urls(url, "ignored", api_key=api_key)
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(models_url)
                if r.status_code != 200:
                    return False, f"http {r.status_code}: {r.text[:200]}"
                payload = r.json()
                models = payload.get("models") or []
                return True, f"{len(models)} models"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    return False, f"unsupported provider: {provider!r}"


# Re-exports so `from monitorul_ii.extraction.enrichments import discourse`
# works as a one-stop import surface.
__all__ = [
    "AnalyzeResult",
    "CallResult",
    "DEFAULT_GOOGLE_AI_STUDIO_URL",
    "DEFAULT_MODEL_BY_PROVIDER",
    "DEFAULT_PROVIDER",
    "GOOGLE_FLASH_LITE_INPUT_RATE_USD",
    "GOOGLE_FLASH_LITE_OUTPUT_RATE_USD",
    "JsonlLogger",
    "PROVIDER_GOOGLE",
    "PROVIDER_OPENROUTER",
    "SUPPORTED_PROVIDERS",
    "call_google_ai_studio",
    "call_llm",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MAX_WORDS",
    "DEFAULT_OPENROUTER_URL",
    "DISCOURSE_MODEL",
    "DISCOURSE_NAMESPACE",
    "DISCOURSE_PRODUCER",
    "DISCOURSE_VERSION",
    "DISCOURSE_VERSION_DOTTED",
    "MIN_TEXT_CHARS",
    "NON_CANONICAL_SPEAKERS",
    "OPENROUTER_FLASH_LITE_INPUT_RATE_USD",
    "OPENROUTER_FLASH_LITE_OUTPUT_RATE_USD",
    "PROMPT_SEQUENCE",
    "PROMPT_VERSIONS",
    "analyze_all",
    "analyze_sidecar",
    "build_prompt_for_speech",
    "build_prompt_for_voice",
    "call_openrouter",
    "discourse_filename",
    "estimate_cost_usd",
    "find_text_offsets",
    "find_text_offsets_tolerant",
    "healthcheck",
    "parse_and_validate",
]


# Tiny silencer: `dataclasses` is imported for forward consistency with
# embedding.py but only `dataclass` / `field` are referenced. Keep the
# import for symmetry; mark its full module with a dunder reference.
_ = dataclasses
