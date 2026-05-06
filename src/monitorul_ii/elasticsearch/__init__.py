"""Elasticsearch projection layer for the canonical sidecar corpus.

ES is *not* the system of record — sidecars on disk + S3 are SOT. This
subpackage holds the v1 mappings, bootstrap helpers, and (in later
phases) the indexer that derives ES docs from `*.extraction.json`
sidecars + parallel enrichment files. See `docs/elasticsearch-indexing.md`
for the design record and `docs/elasticsearch-indexing-prompts.md` for
the per-phase implementation plan.
"""

from __future__ import annotations

from monitorul_ii.elasticsearch.config import ESConfig

__all__ = ["ESConfig"]
