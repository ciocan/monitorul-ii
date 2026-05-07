"""Enrichment producers — sidecar JSON → per-producer enrichment files.

Each module under this subpackage emits a `<basename>.<namespace>.<producer>.v<version>.json`
file alongside its sidecar (or `<basename>.<producer>.v<version>.json` for
producers that don't fan out by model). The indexer's
`monitorul_ii.elasticsearch.enrichments` loader globs these files and
merges them into the ES bulk-upsert flow per Q3 of
`docs/elasticsearch-indexing.md`.

Producers are independently versioned. Adding a new producer means
adding a module here, exporting an `embed_sidecar`-shaped function, and
wiring a CLI subcommand if the operator wants direct access — the
indexer doesn't need to know.
"""

from __future__ import annotations

from monitorul_ii.extraction.enrichments.embedding import (
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_ID,
    EMBEDDING_NAMESPACE,
    EMBEDDING_PRODUCER,
    EMBEDDING_VERSION,
    EMBEDDING_VERSION_DOTTED,
    EmbedResult,
    embed_all,
    embed_sidecar,
    embedding_filename,
)

__all__ = [
    "EMBEDDING_DIMS",
    "EMBEDDING_MODEL",
    "EMBEDDING_MODEL_ID",
    "EMBEDDING_NAMESPACE",
    "EMBEDDING_PRODUCER",
    "EMBEDDING_VERSION",
    "EMBEDDING_VERSION_DOTTED",
    "EmbedResult",
    "embed_all",
    "embed_sidecar",
    "embedding_filename",
]
