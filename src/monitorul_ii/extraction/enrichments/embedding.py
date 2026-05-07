"""BGE-M3 embedding producer.

Walks each sidecar's substantive records — speeches (`is_substantive`
text_length ≥ 100), agenda items (title), interpellations (question_text
+ topic), questions, committee meetings (purpose), and reports (title +
heading outline) — batches their text to the embedding service, and
persists the resulting 1024-dim vectors as

    <basename>.embedding.bge-m3.v0_1.json

next to the sidecar. The indexer's enrichment loader picks up the file,
projects `enrichments.embedding` (dense_vector) and
`enrichments.embedding_text_fingerprint` (keyword) onto each grain doc
that supports them, and the search layer's RRF rank-fusion uses the
vectors at query time (Q8 of `docs/elasticsearch-indexing.md`).

Idempotency contract: each entry stores a `text_fingerprint` (12-char
sha256 of the normalised text). On re-run, entries whose fingerprint
matches the current text are skipped; mismatches are re-embedded so the
hybrid search never serves a vector that doesn't match its text.

Long-tail handling (Q8 v0.1): texts beyond `MAX_TEXT_CHARS` (≈2K
tokens at the BGE-M3 tokeniser) are truncated to first-`MAX_TEXT_CHARS`-
chars-only. The producer marks the entry's `_meta.truncated: true` so
the operator can audit the long-tail size; v0.2 will add proper
chunking with `record_id#chunk-N` keys.

Why a separate process from the FastAPI service: the service holds the
2 GB model in memory; the producer is a thin HTTP client. Decoupling
lets the producer run as a normal `monitorul-ii` subcommand on the
operator's laptop while the service runs on the GPU box, or both run
locally on a CPU box during bootstrap.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import httpx

# Producer identity — these values reach disk in two places:
#
# 1. The filename `<basename>.embedding.<producer>.v<version>.json`.
# 2. The `_meta` block on every per-record entry, where
#    `producer="bge-m3"` records the model identifier (NOT the
#    enrichment namespace) and `version="0.1"` is the producer's own
#    version.
#
# The two-segment filename pattern (`embedding.bge-m3` rather than
# just `bge-m3`) lets the indexer's loader recognise the file as an
# embedding regardless of which model produced it — multiple embedding
# models can coexist for A/B work, each in its own
# `<basename>.embedding.<model>.v<version>.json` file. See the loader
# in `monitorul_ii.elasticsearch.enrichments`.
EMBEDDING_NAMESPACE = "embedding"  # filename + indexer dict key
EMBEDDING_PRODUCER = "bge-m3"  # filename + `_meta.producer`
EMBEDDING_VERSION = "0_1"  # filename version (underscore form)
EMBEDDING_VERSION_DOTTED = "0.1"  # `_meta.version` (dotted form)
EMBEDDING_MODEL = EMBEDDING_PRODUCER  # alias for readability
EMBEDDING_MODEL_ID = "BAAI/bge-m3"  # full HuggingFace model id
EMBEDDING_DIMS = 1024

# v0.1 long-tail handling per Q8: truncate at ~2K tokens. BGE-M3's
# tokeniser averages ~3.8 chars per token on Romanian; 8000 chars is a
# defensive estimate that stays under the 2K-token quality threshold
# for the bulk of plenary speeches. The producer flags the entry's
# `_meta.truncated: true` when this fires so the operator can audit
# how often it's hit before v0.2's chunked-embedding path lands.
MAX_TEXT_CHARS = 8000

# Records below this many chars don't get embedded — they're chair
# turns ("Mulțumesc", "Vă rog") whose vector contributes more noise
# than signal to kNN. Mirrors the indexer's `is_substantive` cutoff
# (`SUBSTANTIVE_TEXT_LENGTH = 100` in `denormalize.py`).
MIN_TEXT_CHARS = 100

# Default batch size sent to the service. The service can re-batch
# internally to stay under GPU memory; this controls how many texts
# travel per HTTP request.
DEFAULT_BATCH_SIZE = 32

# Default endpoint. Override per-call via `embed_url` argument or via
# the `EMBED_URL` environment variable.
DEFAULT_EMBED_URL = "http://127.0.0.1:8000"


@dataclass
class EmbedResult:
    """Outcome of one `embed_sidecar(...)` call.

    `action`:
      - `"embedded"` — at least one record was embedded; the file was
        rewritten.
      - `"skipped"` — every record already had an up-to-date vector;
        no file write.
      - `"dry-run"` — `dry_run=True` short-circuited before write/HTTP.
      - `"error"` — service failure or schema mismatch; see `errors`.

    `embedded` is the count of new vectors written this run; `reused`
    is the count of fingerprint-matched entries kept verbatim;
    `truncated` is the subset of `embedded` whose text was clipped at
    `MAX_TEXT_CHARS`.
    """

    sidecar_path: Path
    document_id: str
    action: str
    embedded: int = 0
    reused: int = 0
    truncated: int = 0
    skipped: int = 0
    file_path: Path | None = None
    errors: list[str] = field(default_factory=list)


def embedding_filename(basename: str) -> str:
    """Compose the canonical embedding filename for a sidecar basename.

    The basename is `<original-pdf-stem>` (i.e. `2018-11-20_MO-PII-168-2018`
    without any suffix). Result:
    `<basename>.embedding.bge-m3.v0_1.json`.
    """
    return f"{basename}.{EMBEDDING_NAMESPACE}.{EMBEDDING_PRODUCER}.v{EMBEDDING_VERSION}.json"


def _resolve_basename(sidecar_path: Path) -> str:
    """Strip the `.extraction.json` tail to recover the basename used
    for sibling enrichment files.
    """
    name = sidecar_path.name
    if name.endswith(".extraction.json"):
        return name[: -len(".extraction.json")]
    return sidecar_path.stem


def _normalise_text(text: str) -> str:
    """NFC + whitespace-collapsed text used for both fingerprinting and
    embedding. Identical normalisation across the two ensures that a
    fingerprint match guarantees the embedded text was the same text.
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFC", text)
    return " ".join(norm.split())


def _text_fingerprint(text: str) -> str:
    """sha256(NFC + whitespace-collapsed text), hex-truncated to 12 chars.

    Same shape as the identity layer's `compute_content_fingerprint` —
    deliberately reused so the producer's idempotency contract aligns
    with the rest of the pipeline. Identical text always produces the
    same fingerprint regardless of cosmetic whitespace.
    """
    digest = hashlib.sha256(_normalise_text(text).encode("utf-8")).hexdigest()
    return digest[:12]


def _truncate(text: str) -> tuple[str, bool]:
    """Cap `text` at `MAX_TEXT_CHARS`. Returns `(maybe_truncated, was_truncated)`."""
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _RecordTask:
    """One record selected for embedding, ready to ship to the service."""

    record_id: str
    text: str
    fingerprint: str
    truncated: bool


def _agenda_item_text(item: dict[str, Any]) -> str:
    """Agenda items embed by their title only — that's the URL-bearing
    decoration the search page surfaces. Body content for an agenda
    lives in its child speeches, which embed independently.
    """
    return str(item.get("title") or "")


def _activity_text(act: dict[str, Any]) -> str:
    """Speech activities embed their `text`. Other activity types
    (vote / procedural / narrator / deferral) are not embedded — votes
    have structured fields, procedurals are chair-procedure noise.
    """
    if act.get("type") != "speech":
        return ""
    return str(act.get("text") or "")


def _interpellation_text(interp: dict[str, Any]) -> str:
    """Interpellations embed `topic` + `question_text` joined; either
    alone is too sparse, both together gives the model enough context
    to disambiguate question subject across the corpus.
    """
    parts: list[str] = []
    topic = interp.get("topic")
    if topic:
        parts.append(str(topic))
    qt = interp.get("question_text")
    if qt:
        parts.append(str(qt))
    return "\n".join(parts)


def _question_text(q: dict[str, Any]) -> str:
    """Questions (qr) embed the same `topic + question_text` join as
    interpellations.
    """
    parts: list[str] = []
    topic = q.get("topic")
    if topic:
        parts.append(str(topic))
    qt = q.get("question_text")
    if qt:
        parts.append(str(qt))
    return "\n".join(parts)


def _meeting_text(meeting: dict[str, Any]) -> str:
    """Committee meetings embed `purpose` + the agenda titles joined by
    newlines. The purpose alone is often boilerplate ("examinarea
    proiectului PL-x N/Y"); the titles add the actual subjects.
    """
    parts: list[str] = []
    purpose = meeting.get("purpose")
    if purpose:
        parts.append(str(purpose))
    for ai in meeting.get("agenda") or []:
        title = ai.get("title") if isinstance(ai, dict) else None
        if title:
            parts.append(str(title))
    return "\n".join(parts)


def _report_text(report: dict[str, Any], headings: list[Any]) -> str:
    """Reports embed `title` + every H1/H2 heading joined by newlines."""
    parts: list[str] = []
    title = report.get("title")
    if title:
        parts.append(str(title))
    for h in headings or []:
        if isinstance(h, dict):
            text = h.get("text")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def _iter_record_tasks(sidecar: dict[str, Any]) -> Iterator[_RecordTask]:
    """Walk every embeddable record across all 6 doc types and yield
    one `_RecordTask` per record whose text length passes
    `MIN_TEXT_CHARS`.

    The walk is duck-typed on `body.<key>[]` — any sidecar shape that
    exposes the canonical keys works, regardless of `document_type`.
    Records below the substantive threshold are silently skipped here;
    the caller's `EmbedResult.skipped` counter doesn't track them
    (they're not "embed-eligible").
    """
    body = sidecar.get("body") or {}
    if not isinstance(body, dict):
        return

    # Plenary / joint session paths
    for item in body.get("agenda_items", []) or []:
        if not isinstance(item, dict):
            continue
        rid = item.get("id")
        if rid:
            text = _agenda_item_text(item)
            if len(text) >= MIN_TEXT_CHARS:
                clipped, was_truncated = _truncate(text)
                yield _RecordTask(
                    record_id=rid,
                    text=clipped,
                    fingerprint=_text_fingerprint(clipped),
                    truncated=was_truncated,
                )
        for act in item.get("activities", []) or []:
            if not isinstance(act, dict):
                continue
            a_rid = act.get("id")
            if not a_rid:
                continue
            text = _activity_text(act)
            if len(text) < MIN_TEXT_CHARS:
                continue
            clipped, was_truncated = _truncate(text)
            yield _RecordTask(
                record_id=a_rid,
                text=clipped,
                fingerprint=_text_fingerprint(clipped),
                truncated=was_truncated,
            )

    for interp in body.get("interpellations", []) or []:
        if not isinstance(interp, dict):
            continue
        rid = interp.get("id")
        if not rid:
            continue
        text = _interpellation_text(interp)
        if len(text) < MIN_TEXT_CHARS:
            continue
        clipped, was_truncated = _truncate(text)
        yield _RecordTask(
            record_id=rid,
            text=clipped,
            fingerprint=_text_fingerprint(clipped),
            truncated=was_truncated,
        )

    # Question-register path
    for q in body.get("questions", []) or []:
        if not isinstance(q, dict):
            continue
        rid = q.get("id")
        if not rid:
            continue
        text = _question_text(q)
        if len(text) < MIN_TEXT_CHARS:
            continue
        clipped, was_truncated = _truncate(text)
        yield _RecordTask(
            record_id=rid,
            text=clipped,
            fingerprint=_text_fingerprint(clipped),
            truncated=was_truncated,
        )

    # Committee-synthesis path
    for committee in body.get("committees", []) or []:
        if not isinstance(committee, dict):
            continue
        for meeting in committee.get("meetings", []) or []:
            if not isinstance(meeting, dict):
                continue
            rid = meeting.get("id")
            if not rid:
                continue
            text = _meeting_text(meeting)
            if len(text) < MIN_TEXT_CHARS:
                continue
            clipped, was_truncated = _truncate(text)
            yield _RecordTask(
                record_id=rid,
                text=clipped,
                fingerprint=_text_fingerprint(clipped),
                truncated=was_truncated,
            )

    # Report-facsimile path
    report = body.get("report")
    if isinstance(report, dict):
        rid = report.get("id")
        if rid:
            text = _report_text(report, body.get("headings") or [])
            if len(text) >= MIN_TEXT_CHARS:
                clipped, was_truncated = _truncate(text)
                yield _RecordTask(
                    record_id=rid,
                    text=clipped,
                    fingerprint=_text_fingerprint(clipped),
                    truncated=was_truncated,
                )


def _existing_entries(path: Path) -> dict[str, dict[str, Any]]:
    """Read the prior embedding file (if any) so we can reuse vectors
    whose fingerprint still matches. Missing or unreadable files behave
    like an empty registry — re-embed everything.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rid, entry in data.items():
        if isinstance(entry, dict):
            out[rid] = entry
    return out


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write to `<path>.part` then rename — same atomic-rename pattern
    used by `extract` / `link` / `backfill`. Producers must never leave
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


def _post_embed(
    client: httpx.Client,
    embed_url: str,
    texts: list[str],
) -> list[list[float]]:
    """One HTTP POST to the embedding service.

    Errors propagate to the caller; `embed_sidecar` records them on
    `EmbedResult.errors` and bails out for the affected sidecar.
    """
    response = client.post(
        embed_url.rstrip("/") + "/embed",
        json={"texts": texts, "normalize": True},
    )
    response.raise_for_status()
    payload = response.json()
    vectors = payload.get("vectors")
    if not isinstance(vectors, list):
        raise ValueError("embed response missing `vectors` array")
    if len(vectors) != len(texts):
        raise ValueError(
            f"embed response length mismatch: got {len(vectors)} for {len(texts)} inputs"
        )
    dims = payload.get("dims")
    if dims and dims != EMBEDDING_DIMS:
        raise ValueError(f"embed response dims={dims} != expected {EMBEDDING_DIMS}")
    return vectors


def _batched(items: list[Any], n: int) -> Iterator[list[Any]]:
    """Yield slices of size `n` from `items`. Final slice is short."""
    if n < 1:
        n = 1
    for i in range(0, len(items), n):
        yield items[i : i + n]


def embed_sidecar(
    sidecar_path: Path,
    *,
    embed_url: str = DEFAULT_EMBED_URL,
    force: bool = False,
    write: bool = True,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    client: httpx.Client | None = None,
) -> EmbedResult:
    """Embed every embeddable record in one sidecar.

    Idempotent: when an existing embedding file carries an entry whose
    `text_fingerprint` matches the current text, the prior vector is
    reused verbatim and no HTTP call is made for that record. Set
    `force=True` to re-embed every record regardless.

    `write=False` skips the disk write (the result still reports what
    *would* have been written); useful for one-shot smoke tests. Pair
    with `dry_run=True` to also skip the HTTP call entirely — pure
    inspection mode.

    `client` is an optional `httpx.Client` for connection reuse across
    many sidecars (the CLI passes one); when None the function builds
    a short-lived one per call.
    """
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return EmbedResult(
            sidecar_path=sidecar_path,
            document_id="",
            action="error",
            errors=[f"read sidecar: {exc}"],
        )

    document_id = sidecar.get("document_id") or ""
    if not document_id:
        return EmbedResult(
            sidecar_path=sidecar_path,
            document_id="",
            action="error",
            errors=["sidecar missing document_id"],
        )

    sidecar_content_sha = sidecar.get("content_sha") or ""

    basename = _resolve_basename(sidecar_path)
    target = sidecar_path.parent / embedding_filename(basename)

    tasks = list(_iter_record_tasks(sidecar))
    if not tasks:
        return EmbedResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="skipped",
            file_path=target if target.exists() else None,
        )

    prior = {} if force else _existing_entries(target)

    embedded: dict[str, dict[str, Any]] = {}
    reused: dict[str, dict[str, Any]] = {}
    needs_embed: list[_RecordTask] = []

    for task in tasks:
        prior_entry = prior.get(task.record_id)
        if (
            not force
            and isinstance(prior_entry, dict)
            and prior_entry.get("text_fingerprint") == task.fingerprint
            and isinstance(prior_entry.get("vector"), list)
            and len(prior_entry["vector"]) == EMBEDDING_DIMS
        ):
            reused[task.record_id] = prior_entry
            continue
        needs_embed.append(task)

    truncated_count = sum(1 for t in needs_embed if t.truncated)

    if dry_run:
        return EmbedResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="dry-run",
            embedded=len(needs_embed),
            reused=len(reused),
            truncated=truncated_count,
            file_path=target,
        )

    if not needs_embed:
        return EmbedResult(
            sidecar_path=sidecar_path,
            document_id=document_id,
            action="skipped",
            embedded=0,
            reused=len(reused),
            file_path=target,
        )

    owns_client = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(120.0))
        owns_client = True

    try:
        for batch in _batched(needs_embed, batch_size):
            texts = [t.text for t in batch]
            try:
                vectors = _post_embed(client, embed_url, texts)
            except Exception as exc:  # noqa: BLE001 — surface any failure
                return EmbedResult(
                    sidecar_path=sidecar_path,
                    document_id=document_id,
                    action="error",
                    errors=[f"embed service: {exc}"],
                    file_path=target,
                )
            now = _now_iso()
            for task, vector in zip(batch, vectors):
                if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMS:
                    return EmbedResult(
                        sidecar_path=sidecar_path,
                        document_id=document_id,
                        action="error",
                        errors=[
                            f"vector for {task.record_id} not {EMBEDDING_DIMS}-dim"
                        ],
                        file_path=target,
                    )
                entry: dict[str, Any] = {
                    "_meta": {
                        "producer": EMBEDDING_PRODUCER,
                        "namespace": EMBEDDING_NAMESPACE,
                        "version": EMBEDDING_VERSION_DOTTED,
                        "model_id": EMBEDDING_MODEL_ID,
                        "dims": EMBEDDING_DIMS,
                        "generated_at": now,
                        "source_sidecar_content_sha": sidecar_content_sha,
                        "truncated": task.truncated,
                    },
                    "vector": vector,
                    "text_fingerprint": task.fingerprint,
                }
                embedded[task.record_id] = entry
    finally:
        if owns_client:
            client.close()

    out: dict[str, dict[str, Any]] = {}
    out.update(reused)
    out.update(embedded)

    if write:
        _atomic_write_json(target, out)

    return EmbedResult(
        sidecar_path=sidecar_path,
        document_id=document_id,
        action="embedded",
        embedded=len(embedded),
        reused=len(reused),
        truncated=truncated_count,
        file_path=target,
    )


def embed_all(
    sidecar_paths: Iterable[Path],
    *,
    embed_url: str = DEFAULT_EMBED_URL,
    force: bool = False,
    write: bool = True,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    client: httpx.Client | None = None,
) -> Iterator[EmbedResult]:
    """Iterate `embed_sidecar(...)` over many sidecars sequentially.

    The HTTP client is reused across calls when supplied — useful for
    bulk runs to avoid TCP handshake per file. If `client` is None,
    a single client is built for the whole batch and torn down at the
    end (the per-call short-lived client behaviour is reserved for the
    single-call signature).

    Errors on individual sidecars don't abort the batch; each
    `EmbedResult` carries its own `errors` list and the caller decides
    how to surface them.
    """
    owns_client = False
    if client is None:
        client = httpx.Client(timeout=httpx.Timeout(120.0))
        owns_client = True

    try:
        for path in sidecar_paths:
            yield embed_sidecar(
                path,
                embed_url=embed_url,
                force=force,
                write=write,
                dry_run=dry_run,
                batch_size=batch_size,
                client=client,
            )
    finally:
        if owns_client:
            client.close()


def healthcheck(embed_url: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """One-shot probe of the embed service.

    Returns `(ok, detail)`. `detail` is the model id on success or a
    short error message on failure. The CLI uses this before walking
    the sidecar list so a misconfigured `EMBED_URL` fails fast instead
    of after N seconds of HTTP errors.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(embed_url.rstrip("/") + "/healthz")
            r.raise_for_status()
            payload = r.json()
            model_id = payload.get("model_id") or "unknown"
            return True, str(model_id)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# Re-exports so `from monitorul_ii.extraction.enrichments import embedding`
# works as a one-stop import surface.
__all__ = [
    "EMBEDDING_DIMS",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_ID",
    "EMBEDDING_NAMESPACE",
    "EMBEDDING_PRODUCER",
    "EMBEDDING_VERSION",
    "EMBEDDING_VERSION_DOTTED",
    "MAX_TEXT_CHARS",
    "MIN_TEXT_CHARS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EMBED_URL",
    "EmbedResult",
    "embed_sidecar",
    "embed_all",
    "embedding_filename",
    "healthcheck",
]
