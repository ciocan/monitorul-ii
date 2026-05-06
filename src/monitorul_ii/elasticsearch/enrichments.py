"""Enrichment file loader — globs `<basename>.<producer>.v<version>.json`
+ the agent journal `<basename>.journal.jsonl` and merges per `record_id`.

Per Q3 of `docs/elasticsearch-indexing.md`, batch-enrichment producers
each write their own file alongside the sidecar; multiple versions can
coexist while a producer rolls out (`topics.v0_1.json` next to
`topics.v0_2.json`). The indexer is the only configured-version
arbiter — `live_versions` decides which version wins for each producer.

Stale-fingerprint filter: each entry's `_meta.source_sidecar_content_sha`
must match the sidecar's current `content_sha`; mismatched entries are
dropped (the producer ran against an older sidecar body).

`enrichment_fingerprint(sidecar_path)` is used by the indexer state
table — it captures the (filename, sha256, mtime) of every enrichment
file so the next pass can short-circuit when nothing has changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

# Filename shape: `<basename>.<producer>.v<version>.json`
# Producer names are kebab-or-snake-case; version is `0_1` / `0_2` etc.
ENRICHMENT_FILENAME_RE = re.compile(
    r"^(?P<basename>.+?)\.(?P<producer>[a-z][a-z0-9_-]*)\.v(?P<version>[0-9_]+)\.json$"
)
JOURNAL_SUFFIX = ".journal.jsonl"
SIDECAR_SUFFIX = ".extraction.json"


def _basename(sidecar_path: Path) -> str:
    """Strip `.extraction.json` to recover the basename used for sibling
    enrichment files. We accept any sidecar-ish path so the public API
    is forgiving.
    """
    name = sidecar_path.name
    if name.endswith(SIDECAR_SUFFIX):
        return name[: -len(SIDECAR_SUFFIX)]
    return sidecar_path.stem


def list_enrichment_files(sidecar_path: Path) -> list[Path]:
    """Return every `<basename>.<producer>.v<version>.json` + the
    journal that lives alongside the sidecar. Sorted for deterministic
    fingerprint computation.
    """
    parent = sidecar_path.parent
    base = _basename(sidecar_path)
    candidates: list[Path] = []
    for child in parent.glob(f"{base}.*"):
        if child.name == sidecar_path.name:
            continue
        if child.name.endswith(JOURNAL_SUFFIX):
            candidates.append(child)
            continue
        m = ENRICHMENT_FILENAME_RE.match(child.name)
        if m and m.group("basename") == base:
            candidates.append(child)
    return sorted(candidates)


def parse_enrichment_filename(path: Path) -> tuple[str, str] | None:
    """Return `(producer, version)` for a per-producer enrichment file.

    Returns None for the journal or anything that doesn't match the
    expected shape.
    """
    m = ENRICHMENT_FILENAME_RE.match(path.name)
    if not m:
        return None
    return m.group("producer"), m.group("version").replace("_", ".")


def _file_sha256(path: Path) -> str:
    """Streaming sha256 — bounded memory for any enrichment file size.

    Reads in 64 KB chunks; even the largest enrichment we expect
    (per-speech embeddings, ~50 MB for a long stenogram) finishes in a
    few hundred ms.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def enrichment_fingerprint(sidecar_path: Path) -> str:
    """Stable-by-content fingerprint of every enrichment file alongside
    the sidecar. Two runs sharing the same fingerprint guarantee that
    every projection-relevant input on disk is identical.

    Mtime is included alongside sha256 so a producer that re-emits the
    same content (rare but possible for a no-op rerun) still bumps the
    fingerprint when it touches the file. Cheap insurance.
    """
    files = list_enrichment_files(sidecar_path)
    if not files:
        return hashlib.sha256(b"empty").hexdigest()
    items: list[str] = []
    for f in files:
        try:
            sha = _file_sha256(f)
        except OSError:
            sha = "missing"
        try:
            mtime = int(f.stat().st_mtime)
        except OSError:
            mtime = 0
        items.append(f"{f.name}|{sha}|{mtime}")
    composite = "\n".join(items).encode("utf-8")
    return hashlib.sha256(composite).hexdigest()


def _version_key(v: str) -> tuple[int, ...]:
    """Sort key for picking the winning version when `live_versions`
    isn't configured. `0.10` should beat `0.2`.
    """
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def load_enrichments(
    sidecar_path: Path,
    *,
    sidecar_content_sha: str | None = None,
    live_versions: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk the sidecar's enrichment files + journal, project them into
    a single `{record_id: {producer: payload}}` map.

    `live_versions` selects which version of each producer wins when
    multiple are present (`{"topics": "0.2"}`). If a producer isn't
    listed, the highest-numbered version on disk wins; this is the
    cheap default during prompt iteration.

    `sidecar_content_sha` enables the stale-fingerprint filter — entries
    whose `_meta.source_sidecar_content_sha` differs from the current
    sidecar's content_sha are dropped, which is how the system handles
    re-extraction (the old enrichment is silently ignored until the
    producer re-runs against the new sidecar).
    """
    files = list_enrichment_files(sidecar_path)
    by_producer: dict[str, list[tuple[str, Path]]] = {}
    journals: list[Path] = []
    for f in files:
        if f.name.endswith(JOURNAL_SUFFIX):
            journals.append(f)
            continue
        parsed = parse_enrichment_filename(f)
        if parsed is None:
            continue
        producer, version = parsed
        by_producer.setdefault(producer, []).append((version, f))

    selected: dict[str, Path] = {}
    for producer, versions in by_producer.items():
        live = (live_versions or {}).get(producer)
        if live:
            for v, p in versions:
                if v == live:
                    selected[producer] = p
                    break
        if producer in selected:
            continue
        # Default: pick the highest version on disk. Ties (impossible
        # given filename uniqueness, but defensive) fall back to last.
        versions.sort(key=lambda vp: _version_key(vp[0]))
        selected[producer] = versions[-1][1]

    out: dict[str, dict[str, Any]] = {}
    for producer, path in selected.items():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for record_id, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            if sidecar_content_sha:
                meta = entry.get("_meta") or {}
                src = (
                    meta.get("source_sidecar_content_sha")
                    if isinstance(meta, dict)
                    else None
                )
                if src and src != sidecar_content_sha:
                    # Stale: producer ran against an older sidecar body.
                    # Drop quietly — the producer will re-emit on its
                    # next run; until then, the indexer projects without
                    # this slice (BM25 still works, kNN drops the
                    # vector — ES dense_vector is sparse-tolerant).
                    continue
            out.setdefault(record_id, {})[producer] = entry

    for journal_path in journals:
        try:
            with journal_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rid = rec.get("record_id")
                    producer = rec.get("producer")
                    if not (rid and producer):
                        continue
                    payload = rec.get("payload") or rec
                    out.setdefault(rid, {}).setdefault("journal", {}).setdefault(
                        producer, []
                    ).append(payload)
        except OSError:
            continue

    return out
