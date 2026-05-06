"""Production indexer — sidecars + enrichments → Elasticsearch.

This module is the routine-trigger codepath from Q6 of
`docs/elasticsearch-indexing.md`. It denormalises each sidecar across
all 9 grains, bulk-upserts via per-grain write aliases, tracks state
in SQLite for idempotency, and runs orphan-delete to drop ES docs whose
record_ids disappeared between runs (e.g. when a re-extract merges two
adjacent speeches).

Design contracts:

* **Idempotency** — `(sidecar_content_sha, enrichment_fingerprint,
  index_generation)` is the state key. Identical triple → skip; any
  one differs → reindex.
* **Orphan-delete** — diff `child_record_ids` (old vs new) and
  `delete_by_query` the missing ones, scoped to this `document_id`.
* **Mirror mode** — `mirror=True` writes to BOTH the live alias AND
  `target=<generation>` so blue-green catch-up runs don't lose new
  ingestion.
* **Webhook** — emits one log line per (grain, id) pair that would be
  invalidated; live HTTP call is gated behind `MONITORUL_ISR_WEBHOOK_URL`
  for now (P5 wires it up properly).

The indexer is sequential by design: each sidecar's work is dominated
by ES bulk + delete-by-query round-trips, so the GIL isn't load-bearing
and process-pool overhead would dwarf the gain. ES bulk-API handles
its own internal batching.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exceptions
from elasticsearch.helpers import bulk as es_bulk

from monitorul_ii.db import DB
from monitorul_ii.elasticsearch.denormalize import (
    child_record_ids,
    denormalize_sidecar,
    to_persons_docs,
)
from monitorul_ii.elasticsearch.enrichments import (
    enrichment_fingerprint,
    load_enrichments,
)

INDEXER_VERSION = "0.1.0"

# All eight grains derived from a sidecar (mo-persons is the registry-
# driven exception and isn't covered by the per-document state row).
_log = logging.getLogger("monitorul_ii.indexer")


@dataclass
class IndexResult:
    """One sidecar's outcome from a `index_one(...)` call.

    `action`:
      - `"indexed"` — ES received the bulk + the state row was upserted
      - `"skipped"` — idempotency triple matched; nothing written
      - `"orphans-only"` — sidecar identical, but old children stale;
        only the delete_by_query ran
    """

    document_id: str
    action: str  # "indexed" | "skipped" | "orphans-only" | "dry-run"
    grain_counts: dict[str, int] = field(default_factory=dict)
    child_record_ids: list[str] = field(default_factory=list)
    orphans_deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _read_sidecar(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _action_for(doc: dict[str, Any], target_index: str | None) -> dict[str, Any]:
    """Translate a denormalised doc into one ES bulk action.

    `target_index` overrides the alias the doc was emitted against —
    used by `--target=` and `--mirror` to write into a specific
    blue-green generation.
    """
    index = target_index if target_index else doc["_index"]
    return {
        "_op_type": "index",
        "_index": index,
        "_id": doc["_id"],
        "_source": doc["_source"],
    }


def _resolve_write_alias(grain: str) -> str:
    """Per Q6: live write target is `<grain>-write` (the alias bootstraps
    point at). Read alias is `<grain>` and is used by the public layer.
    """
    return f"{grain}-write"


def _bulk_upsert(
    es: Elasticsearch,
    docs: list[dict[str, Any]],
    *,
    target: str | None = None,
    mirror: bool = False,
    refresh: str | bool = False,
) -> int:
    """Bulk-upsert `docs` via per-grain write aliases.

    `target` (a single index name like `mo-speeches-20260615-v2`) only
    receives the docs whose `_index` matches its grain prefix — passing
    `target` for `mo-speeches-...` should not redirect `mo-votes` docs.
    `mirror=True` writes BOTH the live alias and `target`.

    Returns the number of successful actions.
    """
    if not docs:
        return 0
    actions: list[dict[str, Any]] = []
    for doc in docs:
        grain = doc["_index"]
        write_alias = _resolve_write_alias(grain)
        if target and target.startswith(grain + "-"):
            if mirror:
                actions.append(_action_for(doc, write_alias))
                actions.append(_action_for(doc, target))
            else:
                actions.append(_action_for(doc, target))
        else:
            actions.append(_action_for(doc, write_alias))
    success, _errors = es_bulk(
        es,
        actions,
        refresh=refresh,
        raise_on_error=True,
        # Stats-only=False so the second tuple element (errors) is
        # populated; we let raise_on_error=True turn any failure into
        # an exception that the caller can record on IndexResult.
        stats_only=False,
    )
    return success


def _delete_orphans(
    es: Elasticsearch,
    *,
    document_id: str,
    grain: str,
    record_ids: list[str],
    target: str | None = None,
    mirror: bool = False,
    refresh: str | bool = True,
) -> int:
    """`delete_by_query` over `<grain>-write` (or `target`) for the
    given `record_ids`, scoped to this document_id.

    The document_id scope is defensive: even if record_ids drift across
    documents (which shouldn't happen — record_ids embed document_id
    per Q2), we never accidentally delete a sibling's record.
    """
    if not record_ids:
        return 0
    write_alias = _resolve_write_alias(grain)

    body = {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"document_id": document_id}},
                    {"terms": {"record_id": record_ids}},
                ]
            }
        }
    }

    deleted = 0
    targets: list[str] = []
    if target and target.startswith(grain + "-"):
        targets.append(target)
        if mirror:
            targets.append(write_alias)
    else:
        targets.append(write_alias)

    for tgt in targets:
        try:
            resp = es.delete_by_query(
                index=tgt,
                **body,
                refresh=bool(refresh),
                conflicts="proceed",
            )
        except es_exceptions.NotFoundError:
            continue
        deleted += int(resp.get("deleted", 0) or 0)
    return deleted


def _diff_orphans(old: list[str], new: list[str]) -> list[str]:
    """Record_ids that existed last run but are gone this run."""
    new_set = set(new)
    return [rid for rid in old if rid not in new_set]


def _emit_invalidation(grain: str, record_id: str) -> None:
    """Stub for the ISR invalidation webhook (P5 wiring).

    For now we only log. Set MONITORUL_ISR_WEBHOOK_URL to opt into the
    real HTTP call once the Next.js side is wired; until then, the log
    line is enough to trace which (grain, record_id) pairs would have
    invalidated on a real production run.
    """
    if os.environ.get("MONITORUL_ISR_WEBHOOK_URL"):
        # Live HTTP call deferred to P5; for now we still log so the
        # upcoming integration has audit-shaped output to verify against.
        _log.info(
            "isr_invalidate_pending",
            extra={"grain": grain, "record_id": record_id},
        )
    else:
        _log.info("isr_invalidate_stub grain=%s record_id=%s", grain, record_id)


def index_one(
    es: Elasticsearch,
    db: DB,
    sidecar_path: Path,
    *,
    target: str | None = None,
    mirror: bool = False,
    force: bool = False,
    dry_run: bool = False,
    grains: tuple[str, ...] | None = None,
    index_generation: str = "live",
    s3_urls: dict[str, str] | None = None,
    live_versions: dict[str, str] | None = None,
    refresh: str | bool = False,
) -> IndexResult:
    """Index one sidecar end-to-end.

    `target` selects an explicit blue-green generation to write into
    (e.g. `mo-speeches-20260615-v2` — but the indexer rotates through
    all matching grains, so pass the grain-prefix to direct only that
    grain's docs). `mirror=True` keeps the live alias in sync.

    `dry_run=True` runs the entire pipeline up to the bulk call without
    contacting ES. `grains` filters the projection to a subset
    (`("mo-documents",)` for the documents-only re-pass on a major
    trigger).

    `index_generation` is the third leg of the idempotency triple — by
    default `"live"`, override with the bootstrap suffix when running
    `--target` so the state row's idempotency check tracks the right
    generation.
    """
    sidecar = _read_sidecar(sidecar_path)
    document_id = sidecar.get("document_id")
    if not document_id:
        return IndexResult(
            document_id=str(sidecar_path),
            action="error",
            errors=["sidecar missing document_id"],
        )

    sidecar_content_sha = sidecar.get("content_sha") or ""
    enrich_fingerprint = enrichment_fingerprint(sidecar_path)
    enrichments = load_enrichments(
        sidecar_path,
        sidecar_content_sha=sidecar_content_sha,
        live_versions=live_versions,
    )

    state = db.get_indexed_state(document_id)
    if (
        state is not None
        and not force
        and state.get("sidecar_content_sha") == sidecar_content_sha
        and state.get("enrichment_fingerprint") == enrich_fingerprint
        and state.get("index_generation") == index_generation
    ):
        return IndexResult(
            document_id=document_id,
            action="skipped",
            child_record_ids=list(state.get("child_record_ids") or []),
        )

    docs = denormalize_sidecar(
        sidecar,
        enrichments=enrichments,
        grains=grains,
        s3_urls=s3_urls,
        indexed_at=_now_iso(),
    )
    grouped = child_record_ids(docs)
    new_record_ids = sorted({rid for ids in grouped.values() for rid in ids})

    if dry_run:
        return IndexResult(
            document_id=document_id,
            action="dry-run",
            grain_counts={g: len(ids) for g, ids in grouped.items()},
            child_record_ids=new_record_ids,
        )

    errors: list[str] = []
    grain_counts: dict[str, int] = {}
    try:
        success = _bulk_upsert(es, docs, target=target, mirror=mirror, refresh=refresh)
        # _bulk_upsert returns total actions queued; per-grain breakdown
        # for the result comes from `grouped`.
        for g, ids in grouped.items():
            grain_counts[g] = len(ids)
        # Only used to detect "all bulk actions failed" — raise_on_error
        # would normally throw before this branch.
        if success == 0 and docs:
            errors.append("bulk returned 0 successes")
    except Exception as exc:  # noqa: BLE001 — bubble the cause to result
        errors.append(f"bulk error: {exc}")
        return IndexResult(
            document_id=document_id,
            action="error",
            grain_counts=grain_counts,
            child_record_ids=new_record_ids,
            errors=errors,
        )

    # Orphan delete: per grain, drop any record_id that was indexed
    # last time and isn't in this run's grouped output.
    old_ids = list((state or {}).get("child_record_ids") or [])
    orphans_per_grain: dict[str, list[str]] = {}
    if old_ids and grains is None:
        # Group old ids by grain prefix from `record_id` shape — every
        # record_id starts with `mo://...#<seg>` where the seg root
        # determines the grain. The denormalize.child_record_ids() shape
        # for the *new* state already groups; we re-group the old set
        # by the same convention.
        old_by_grain = _regroup_old(old_ids, grouped, document_id)
        for grain, old_grain_ids in old_by_grain.items():
            current = grouped.get(grain) or []
            orphans = _diff_orphans(old_grain_ids, current)
            if orphans:
                orphans_per_grain[grain] = orphans

    orphans_deleted = 0
    for grain, orphan_ids in orphans_per_grain.items():
        try:
            orphans_deleted += _delete_orphans(
                es,
                document_id=document_id,
                grain=grain,
                record_ids=orphan_ids,
                target=target,
                mirror=mirror,
                refresh=True,
            )
            for rid in orphan_ids:
                _emit_invalidation(grain, rid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"orphan-delete {grain} error: {exc}")

    db.set_indexed_state(
        document_id,
        sidecar_content_sha=sidecar_content_sha,
        enrichment_fingerprint=enrich_fingerprint,
        index_generation=index_generation,
        child_record_ids=new_record_ids,
    )

    for grain, ids in grouped.items():
        for rid in ids:
            _emit_invalidation(grain, rid)

    return IndexResult(
        document_id=document_id,
        action="indexed",
        grain_counts=grain_counts,
        child_record_ids=new_record_ids,
        orphans_deleted=orphans_deleted,
        errors=errors,
    )


def _regroup_old(
    old_ids: list[str],
    new_grouped: dict[str, list[str]],
    document_id: str,
) -> dict[str, list[str]]:
    """Recover a grain mapping for the previous run's record_ids.

    The id shape is unambiguous (Q2 of the design doc): we walk each
    old id through `_guess_grain_from_id` for grain attribution. The
    document_id scope on `_delete_orphans` means a misattribution
    would still be safe — but the attribution is deterministic from
    the id shape, so this is just defensive belt-and-suspenders.
    """
    fallback: dict[str, list[str]] = {}
    for old in old_ids:
        grain = _guess_grain_from_id(old, document_id)
        if grain:
            fallback.setdefault(grain, []).append(old)
    return fallback


def _guess_grain_from_id(record_id: str, document_id: str) -> str | None:
    """Best-effort grain attribution for orphan-delete fallback.

    `record_id` shapes per Q2:
      - `mo://YYYY/PART/ISSUE` → `mo-documents`
      - `...#agenda-N` → `mo-agenda-items`
      - `...#agenda-N#act-M` → `mo-speeches`
      - `...#agenda-N#vote-M` → `mo-votes`
      - `...#interp-N` → `mo-interpellations`
      - `...#q-N` → `mo-questions`
      - `...#cmt-...` → `mo-committee-meetings`
    """
    if record_id == document_id:
        return "mo-documents"
    if "#vote-" in record_id:
        return "mo-votes"
    if "#act-" in record_id:
        return "mo-speeches"
    if "#agenda-" in record_id:
        return "mo-agenda-items"
    if "#interp-" in record_id:
        return "mo-interpellations"
    if "#q-" in record_id:
        return "mo-questions"
    if "#cmt-" in record_id:
        return "mo-committee-meetings"
    if record_id.startswith("mo://"):
        # Could be a report (doc-level id without `#`)
        return "mo-reports"
    return None


def index_all(
    es: Elasticsearch,
    db: DB,
    sidecar_paths: list[Path],
    *,
    target: str | None = None,
    mirror: bool = False,
    force: bool = False,
    dry_run: bool = False,
    grains: tuple[str, ...] | None = None,
    index_generation: str = "live",
    live_versions: dict[str, str] | None = None,
    s3_urls_for: object | None = None,
    refresh: str | bool = False,
) -> Iterator[IndexResult]:
    """Iterate over `sidecar_paths`, yielding one IndexResult per file.

    Sequential — yields in input order. For parallel execution see
    `index_all_parallel(...)`; the trade-off is documented there.

    `s3_urls_for` is an optional callable `(sidecar_path) -> dict | None`
    that resolves the per-document S3 pointer triple for the
    `mo-documents` doc. Default None (no S3 pointers projected).
    """
    for path in sidecar_paths:
        urls = None
        if s3_urls_for is not None:
            try:
                urls = s3_urls_for(path)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                urls = None
        result = index_one(
            es,
            db,
            path,
            target=target,
            mirror=mirror,
            force=force,
            dry_run=dry_run,
            grains=grains,
            index_generation=index_generation,
            s3_urls=urls,
            live_versions=live_versions,
            refresh=refresh,
        )
        yield result


def index_all_parallel(
    es: Elasticsearch,
    db_path: Path,
    sidecar_paths: list[Path],
    *,
    workers: int,
    target: str | None = None,
    mirror: bool = False,
    force: bool = False,
    dry_run: bool = False,
    grains: tuple[str, ...] | None = None,
    index_generation: str = "live",
    live_versions: dict[str, str] | None = None,
    s3_urls_for: object | None = None,
    refresh: str | bool = False,
) -> Iterator[IndexResult]:
    """Thread-pooled variant of `index_all`. Yields results in
    *completion* order, not input order — same convention as the
    backfill / convert parallel paths.

    Why threads (not processes): per-sidecar work is dominated by ES
    network round-trips (bulk + delete_by_query); both release the GIL
    via urllib3, so threads scale linearly with worker count up to the
    cluster's bulk-throughput ceiling. ProcessPool would also work but
    pays serialisation cost per task and forces re-construction of the
    ES client + DB connection on every worker spawn — pure overhead
    for this workload.

    Why per-thread DB connections: SQLite connections aren't safe to
    share across threads (the `sqlite3` module enforces this; raises
    `ProgrammingError` on cross-thread use). Each worker opens its own
    `DB(db_path)` connection on first use; WAL mode handles concurrent
    readers + serialised writers fine at this write rate (one row per
    sidecar, microseconds per write while ES round-trips are 500 ms+).

    Falls back to the sequential `index_all(...)` when `workers <= 1`
    or `len(sidecar_paths) == 1` so callers don't pay pool-setup cost
    for trivial workloads.
    """
    if workers <= 1 or len(sidecar_paths) <= 1:
        # Sequential path — open one DB connection here so the caller
        # doesn't have to manage it. Mirrors the convert / backfill
        # short-circuit pattern.
        db = DB(db_path)
        try:
            yield from index_all(
                es,
                db,
                sidecar_paths,
                target=target,
                mirror=mirror,
                force=force,
                dry_run=dry_run,
                grains=grains,
                index_generation=index_generation,
                live_versions=live_versions,
                s3_urls_for=s3_urls_for,
                refresh=refresh,
            )
        finally:
            db.close()
        return

    # Per-thread DB cache — one connection per worker thread, reused
    # across that thread's tasks for the duration of the run. Each
    # connection is closed on pool teardown via the workers list.
    local = threading.local()
    workers_dbs: list[DB] = []
    workers_dbs_lock = threading.Lock()

    def _get_thread_db() -> DB:
        existing = getattr(local, "db", None)
        if existing is not None:
            return existing
        new_db = DB(db_path)
        with workers_dbs_lock:
            workers_dbs.append(new_db)
        local.db = new_db
        return new_db

    def _run_one(path: Path) -> IndexResult:
        thread_db = _get_thread_db()
        urls = None
        if s3_urls_for is not None:
            try:
                urls = s3_urls_for(path)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                urls = None
        return index_one(
            es,
            thread_db,
            path,
            target=target,
            mirror=mirror,
            force=force,
            dry_run=dry_run,
            grains=grains,
            index_generation=index_generation,
            s3_urls=urls,
            live_versions=live_versions,
            refresh=refresh,
        )

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mo-index")
    futures = [executor.submit(_run_one, p) for p in sidecar_paths]
    try:
        for fut in as_completed(futures):
            yield fut.result()
    finally:
        # `wait=False, cancel_futures=True` mirrors the backfill pattern
        # so a Ctrl+C lets in-flight tasks finish their current sidecar
        # atomically without blocking on a queue that might still be
        # spinning up workers.
        executor.shutdown(wait=False, cancel_futures=True)
        for d in workers_dbs:
            try:
                d.close()
            except Exception:  # noqa: BLE001
                pass


def index_persons_registry(
    es: Elasticsearch,
    persons: list[dict[str, Any]] | dict[str, Any],
    *,
    target: str | None = None,
    refresh: str | bool = "wait_for",
) -> int:
    """Bulk-index the persons registry into `mo-persons`.

    Distinct from `index_one(...)` because persons aren't sidecar-derived
    — they come straight from `persons.json`. Used by `--include-persons`
    on the index CLI; routine sidecar indexer runs leave `mo-persons`
    alone.

    Returns the count of successfully indexed person docs.
    """
    docs = to_persons_docs(persons)
    if not docs:
        return 0
    actions = []
    write_alias = _resolve_write_alias("mo-persons")
    for doc in docs:
        actions.append(_action_for(doc, target if target else write_alias))
    success, _ = es_bulk(es, actions, refresh=refresh, raise_on_error=True)
    return success


# Sentinel document_id used by the persons-projection state row in
# `es_indexed`. Distinct from any real `mo://YYYY/PART/ISSUE` id so
# the persons row never collides with a sidecar's row even if a
# pathological MO ever shipped under this URI.
PERSONS_STATE_KEY = "__persons_registry__"


def _persons_fingerprint(persons: dict[str, Any] | list[dict[str, Any]]) -> str:
    """Stable content hash for the persons registry, used as the
    `sidecar_content_sha` analogue in the `es_indexed` state row.

    sha256-hex over a canonical JSON serialisation: sorted keys + no
    whitespace. The same registry will always hash the same regardless
    of dict-iteration order. Recomputed on every CLI run so we don't
    cache a stale fingerprint when the operator hand-edits the file.
    """
    payload = json.dumps(persons, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def index_persons_with_state(
    es: Elasticsearch,
    db: DB,
    persons: dict[str, Any] | list[dict[str, Any]],
    *,
    target: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    index_generation: str = "live",
    refresh: str | bool = "wait_for",
) -> IndexResult:
    """Project the persons registry to ES with idempotency tracking.

    Mirrors `index_one`'s shape: computes a fingerprint over the
    persons content + checks the `es_indexed` state row keyed by
    `PERSONS_STATE_KEY`; skips on a triple match unless `force=True`.

    Orphan-delete: when a registry entry is removed (rare — happens
    after a stub-merge cleanup), the diff old vs new pulls the
    abandoned `_id` out of `mo-persons` so the public site doesn't
    serve a stale `/politicieni/<slug>` page.
    """
    docs = to_persons_docs(persons)
    new_record_ids = sorted({d["_id"] for d in docs})
    fingerprint = _persons_fingerprint(persons)

    state = db.get_indexed_state(PERSONS_STATE_KEY)
    if (
        state is not None
        and not force
        and state.get("sidecar_content_sha") == fingerprint
        and state.get("index_generation") == index_generation
    ):
        return IndexResult(
            document_id=PERSONS_STATE_KEY,
            action="skipped",
            child_record_ids=list(state.get("child_record_ids") or []),
        )

    if dry_run:
        return IndexResult(
            document_id=PERSONS_STATE_KEY,
            action="dry-run",
            grain_counts={"mo-persons": len(docs)},
            child_record_ids=new_record_ids,
        )

    errors: list[str] = []
    grain_counts: dict[str, int] = {"mo-persons": 0}
    if docs:
        try:
            actions = [
                _action_for(d, target if target else _resolve_write_alias("mo-persons"))
                for d in docs
            ]
            success, _ = es_bulk(es, actions, refresh=refresh, raise_on_error=True)
            grain_counts["mo-persons"] = success
        except Exception as exc:  # noqa: BLE001 — bubble to result
            errors.append(f"persons bulk error: {exc}")
            return IndexResult(
                document_id=PERSONS_STATE_KEY,
                action="error",
                grain_counts=grain_counts,
                child_record_ids=new_record_ids,
                errors=errors,
            )

    # Orphan-delete: persons removed since last run.
    old_ids = list((state or {}).get("child_record_ids") or [])
    orphans = _diff_orphans(old_ids, new_record_ids)
    orphans_deleted = 0
    if orphans:
        try:
            orphans_deleted = _delete_orphans(
                es,
                document_id=PERSONS_STATE_KEY,
                grain="mo-persons",
                record_ids=orphans,
                target=target,
                mirror=False,
                refresh=True,
            )
            for rid in orphans:
                _emit_invalidation("mo-persons", rid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"persons orphan-delete error: {exc}")

    db.set_indexed_state(
        PERSONS_STATE_KEY,
        sidecar_content_sha=fingerprint,
        enrichment_fingerprint="n/a",
        index_generation=index_generation,
        child_record_ids=new_record_ids,
    )
    for rid in new_record_ids:
        _emit_invalidation("mo-persons", rid)

    return IndexResult(
        document_id=PERSONS_STATE_KEY,
        action="indexed",
        grain_counts=grain_counts,
        child_record_ids=new_record_ids,
        orphans_deleted=orphans_deleted,
        errors=errors,
    )
