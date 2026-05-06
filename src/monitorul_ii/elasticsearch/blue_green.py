"""Blue-green helpers for major-trigger index lifecycle (Q6).

Routine triggers (daily ingestion, re-extraction, linker reruns,
enrichment producer bumps) flow through `indexer.index_one(...)` against
the existing write alias. Major triggers (mapping changes, schema
breaking-changes) cut a new generation:

  1. `create_target_generation(es, grains, suffix)` — mint
     `mo-<grain>-<suffix>` indices alongside the live ones; refresh
     interval set to `-1` so the bulk-load doesn't fight Lucene merges.
  2. `add_to_write_alias(es, grain, generation)` — flip the write alias
     to send writes to BOTH the live and target indices simultaneously
     (`is_write_index` rotated). The indexer's `--mirror` mode keeps
     the target current while the bulk catch-up runs.
  3. (operator runs `monitorul-ii index --rebuild` or
     `--mirror --target=<generation>` until target ≈ live)
  4. `swap_read_alias(es, grain, generation)` — atomic alias update
     that points the read alias at the target. Public reads switch in
     one ES call; rollback is the inverse swap.
  5. `drop_old_generation(es, grain, generation)` — post-cooldown
     cleanup of the now-unaliased indices. Only call after the read
     alias has cut over and the operator is satisfied.

This module deliberately stays thin — every helper is one ES call plus
a tiny preflight check, so an operator can read the source and
understand what's about to happen. Anything more elaborate (multi-step
transactions, retry logic) belongs in the indexer or in a dedicated
operator script.
"""

from __future__ import annotations

from importlib import resources
import json
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exceptions

from monitorul_ii.elasticsearch.bootstrap import GRAINS

# Exposed for callers that want to gate on the canonical grain list
# without re-importing from bootstrap.
__all__ = [
    "GRAINS",
    "create_target_generation",
    "add_to_write_alias",
    "swap_read_alias",
    "drop_old_generation",
    "list_generations",
]


def _load_mapping_body(grain: str) -> dict[str, Any]:
    """Load the per-grain mapping JSON. The bootstrap helpers do the
    same thing privately; we duplicate the loader here so callers don't
    have to import bootstrap internals.
    """
    pkg = "monitorul_ii.elasticsearch.mappings"
    text = resources.files(pkg).joinpath(f"{grain}.json").read_text(encoding="utf-8")
    return json.loads(text)


def _index_for_generation(grain: str, generation: str) -> str:
    """Compose the concrete index name. The generation suffix is the
    keystone — `mo-speeches-20260615-v2` is unambiguously the v2
    mapping cut on June 15, 2026.
    """
    return f"{grain}-{generation}"


def create_target_generation(
    es: Elasticsearch,
    grains: list[str],
    suffix: str,
    *,
    refresh_interval: str = "-1",
) -> list[str]:
    """Create new versioned indices for the selected grains.

    `refresh_interval='-1'` disables auto-refresh during the bulk-load
    so the indexer can stream millions of docs without paying merge
    cost on each refresh tick. The operator must explicitly refresh
    (or set the interval back to `30s`) before swapping the read alias
    or queries will see no data.

    Returns the list of index names created. Already-present indices
    are left untouched and excluded from the return value — call
    `list_generations` if you need the full picture afterwards.
    """
    created: list[str] = []
    for grain in grains:
        if grain not in GRAINS:
            raise ValueError(f"unknown grain: {grain!r}")
        index_name = _index_for_generation(grain, suffix)
        if es.indices.exists(index=index_name):
            continue
        # Index template wiring (`<grain>-template`) handles the bulk
        # of the mapping; we override the refresh interval at create
        # time so the mass-load doesn't churn segments.
        body = _load_mapping_body(grain)
        template = body.get("template") or {}
        settings = dict(template.get("settings") or {})
        settings["refresh_interval"] = refresh_interval
        es.indices.create(
            index=index_name,
            mappings=template.get("mappings"),
            settings=settings,
        )
        created.append(index_name)
    return created


def add_to_write_alias(es: Elasticsearch, grain: str, generation: str) -> None:
    """Atomically add the target index to the grain's write alias.

    The target becomes a co-write target (`is_write_index: true`) so
    every routine indexer pass writes to BOTH the live and target
    indices — that's how Q6's "dual-write during catch-up" works.
    The live index remains in the alias but loses its is_write_index
    flag, so reads still hit it but new writes also flow to target.
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain: {grain!r}")
    target = _index_for_generation(grain, generation)
    write_alias = f"{grain}-write"

    # Get the current write-alias targets so we can demote them in
    # the same atomic update_aliases call.
    actions: list[dict[str, Any]] = []
    try:
        existing = es.indices.get_alias(name=write_alias)
    except es_exceptions.NotFoundError:
        existing = {}
    for idx_name in (existing or {}).keys():
        actions.append(
            {
                "add": {
                    "index": idx_name,
                    "alias": write_alias,
                    "is_write_index": False,
                }
            }
        )
    actions.append(
        {
            "add": {
                "index": target,
                "alias": write_alias,
                "is_write_index": True,
            }
        }
    )
    es.indices.update_aliases(actions=actions)


def swap_read_alias(es: Elasticsearch, grain: str, generation: str) -> None:
    """Atomically swap the grain's read alias to the target generation.

    All public reads cut over in one ES call. The previously-live
    index is removed from the read alias (still present in the
    write alias unless `drop_old_generation` is called next).
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain: {grain!r}")
    target = _index_for_generation(grain, generation)
    read_alias = grain
    actions: list[dict[str, Any]] = []
    try:
        existing = es.indices.get_alias(name=read_alias)
    except es_exceptions.NotFoundError:
        existing = {}
    for idx_name in (existing or {}).keys():
        if idx_name == target:
            continue
        actions.append({"remove": {"index": idx_name, "alias": read_alias}})
    actions.append({"add": {"index": target, "alias": read_alias}})
    es.indices.update_aliases(actions=actions)


def drop_old_generation(es: Elasticsearch, grain: str, generation: str) -> None:
    """Delete the named generation's index. No alias check — the
    operator is presumed to have already swapped the read + write
    aliases and waited out a cooldown period. Use with care; this is
    the only destructive call in the module.
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain: {grain!r}")
    index_name = _index_for_generation(grain, generation)
    if not es.indices.exists(index=index_name):
        return
    es.indices.delete(index=index_name)


def list_generations(es: Elasticsearch, grain: str) -> list[str]:
    """Return every generation suffix currently present for `grain`.

    Useful for the operator deciding which old generations are safe to
    drop. The list is sorted alphabetically — generations are dated, so
    that's chronological too.
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain: {grain!r}")
    pattern = f"{grain}-*"
    try:
        resp = es.indices.get(index=pattern)
    except es_exceptions.NotFoundError:
        return []
    out: list[str] = []
    for index_name in resp.keys():
        if not index_name.startswith(grain + "-"):
            continue
        out.append(index_name[len(grain) + 1 :])
    return sorted(out)
