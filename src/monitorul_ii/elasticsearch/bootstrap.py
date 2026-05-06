"""Provision Elasticsearch templates, indices, aliases, and API keys.

The bootstrap is idempotent: re-running detects existing component
templates / index templates / indices / aliases and no-ops. The blue-
green generation suffix (`mo-<grain>-<YYYYMMDD>-v<n>`) is computed once
per `create_indices` call; subsequent runs that find an alias already
pointed at a generation skip creating a new one. Mapping changes are
out-of-scope for the bootstrap — that's a major-trigger blue-green
operation handled by the indexer (Q6 in the design doc).

The mapping JSONs under `mappings/` are the single source of truth for
v1 field shapes; this module only loads + dispatches them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exceptions

# Component templates — shared analyzers + common fields composed into
# every grain's index template. Keep the names cluster-stable; renaming
# breaks every existing index template that composes them.
COMPONENT_ANALYZERS = "mo-analyzers"
COMPONENT_COMMON_FIELDS = "mo-common-fields"

# Index grains. Order matters only for deterministic logging output.
GRAINS: tuple[str, ...] = (
    "mo-documents",
    "mo-agenda-items",
    "mo-speeches",
    "mo-votes",
    "mo-interpellations",
    "mo-questions",
    "mo-committee-meetings",
    "mo-reports",
    "mo-persons",
)

API_KEY_READER = "monitorul_reader"
API_KEY_INDEXER = "monitorul_indexer"


@dataclass(frozen=True)
class CreatedEntity:
    """One result row reported by the bootstrap helpers.

    `created` is True when this run actually wrote to ES; False when the
    entity already existed and was left untouched (idempotent path).
    """

    name: str
    kind: str
    created: bool
    detail: str | None = None


def _load_mapping(filename: str) -> dict[str, Any]:
    """Read a JSON mapping bundled with the package."""
    pkg = "monitorul_ii.elasticsearch.mappings"
    text = resources.files(pkg).joinpath(filename).read_text(encoding="utf-8")
    return json.loads(text)


def _component_template_body(filename: str) -> dict[str, Any]:
    """ES `put_component_template` expects the body to wrap the
    template under a `template` key. The mapping JSONs use the same
    `{settings, mappings}` envelope that index templates do, so we
    rewrap into the component-template shape here.
    """
    raw = _load_mapping(filename)
    template: dict[str, Any] = {}
    if "settings" in raw:
        template["settings"] = raw["settings"]
    if "mappings" in raw:
        template["mappings"] = raw["mappings"]
    return {"template": template}


def create_component_templates(es: Elasticsearch) -> list[CreatedEntity]:
    """Install the two cluster-level component templates referenced by
    every index template (`mo-analyzers`, `mo-common-fields`).
    Idempotent — already-present templates are skipped.
    """
    out: list[CreatedEntity] = []
    plan = (
        (COMPONENT_ANALYZERS, "_analyzers.json"),
        (COMPONENT_COMMON_FIELDS, "_common_fields.json"),
    )
    for name, filename in plan:
        if es.cluster.exists_component_template(name=name):
            out.append(
                CreatedEntity(name=name, kind="component_template", created=False)
            )
            continue
        body = _component_template_body(filename)
        es.cluster.put_component_template(name=name, **body)
        out.append(CreatedEntity(name=name, kind="component_template", created=True))
    return out


def create_index_templates(es: Elasticsearch) -> list[CreatedEntity]:
    """Install one index template per grain. Each composes
    `mo-analyzers` and (where applicable) `mo-common-fields` and adds
    its grain-specific properties.
    """
    out: list[CreatedEntity] = []
    for grain in GRAINS:
        template_name = f"{grain}-template"
        if es.indices.exists_index_template(name=template_name):
            out.append(
                CreatedEntity(name=template_name, kind="index_template", created=False)
            )
            continue
        body = _load_mapping(f"{grain}.json")
        # `put_index_template` consumes index_patterns / composed_of /
        # template at the top level — same shape as our JSON files.
        es.indices.put_index_template(name=template_name, **body)
        out.append(
            CreatedEntity(name=template_name, kind="index_template", created=True)
        )
    return out


def _generation_suffix(now: datetime | None = None) -> str:
    """`YYYYMMDD-v1` for the v1 bootstrap. Future major-trigger
    rebuilds (Q6) bump the `v<n>` portion; the date prefix lets a
    single index name encode both *when* it was created and *which*
    schema generation it belongs to.
    """
    n = now or datetime.now(timezone.utc)
    return f"{n:%Y%m%d}-v1"


def _resolve_existing_alias_target(es: Elasticsearch, alias: str) -> str | None:
    """Return the index name this alias currently points at, or None
    if the alias does not exist. ES returns 404 (NotFoundError) for
    missing aliases, which we map to None so callers can branch on
    `existing is None`.
    """
    try:
        resp = es.indices.get_alias(name=alias)
    except es_exceptions.NotFoundError:
        return None
    # `resp` is a dict keyed by index name. The alias contract here is
    # one-to-one (one read alias → one live generation), so we take
    # the first key.
    keys = list(resp.keys()) if hasattr(resp, "keys") else []
    return keys[0] if keys else None


def create_indices(
    es: Elasticsearch, *, generation_suffix: str | None = None
) -> list[CreatedEntity]:
    """Create one concrete index per grain, plus a read alias
    `<grain>` and a write alias `<grain>-write` per the blue-green
    Q6 lifecycle. Idempotent: when the read alias already points at
    a live generation, that generation is preserved and no new index
    is created.

    `generation_suffix` defaults to today's `YYYYMMDD-v1`; pass an
    explicit value (e.g. `20260615-v2`) when scripting a major-trigger
    rebuild.
    """
    suffix = generation_suffix or _generation_suffix()
    out: list[CreatedEntity] = []
    for grain in GRAINS:
        read_alias = grain
        write_alias = f"{grain}-write"
        existing = _resolve_existing_alias_target(es, read_alias)
        if existing is not None:
            out.append(
                CreatedEntity(
                    name=existing,
                    kind="index",
                    created=False,
                    detail=f"alias {read_alias} → {existing}",
                )
            )
            continue
        index_name = f"{grain}-{suffix}"
        # The aliases ride on the index creation request so they appear
        # atomically with the index itself — no window where the alias
        # is unset. is_write_index marks the write alias as the single
        # designated write target (matters when multiple generations
        # share an alias during blue-green catch-up).
        es.indices.create(
            index=index_name,
            aliases={
                read_alias: {},
                write_alias: {"is_write_index": True},
            },
        )
        out.append(
            CreatedEntity(
                name=index_name,
                kind="index",
                created=True,
                detail=(f"aliases: {read_alias} (read), {write_alias} (write)"),
            )
        )
    return out


def _reader_role_descriptor() -> dict[str, Any]:
    """`monitorul_reader`: read-only on `mo-*` indices.

    No scripting, no scroll API, no `_sql`, no cluster info — keeps
    the public website's surface narrow per Q9. ES role descriptors
    take wildcard index patterns directly; cluster privileges are
    omitted to deny everything cluster-level by default.
    """
    return {
        "cluster": [],
        "indices": [
            {
                "names": ["mo-*"],
                "privileges": ["read", "view_index_metadata"],
                "allow_restricted_indices": False,
            }
        ],
    }


def _indexer_role_descriptor() -> dict[str, Any]:
    """`monitorul_indexer`: read+write on `mo-*` indices.

    `manage` is included so the indexer can refresh / update aliases
    during blue-green swaps. Cluster admin stays denied — the
    indexer never reaches outside its index family.
    """
    return {
        "cluster": ["monitor"],
        "indices": [
            {
                "names": ["mo-*"],
                "privileges": [
                    "read",
                    "write",
                    "create",
                    "create_index",
                    "manage",
                    "view_index_metadata",
                ],
                "allow_restricted_indices": False,
            }
        ],
    }


def _api_key_exists(es: Elasticsearch, name: str) -> bool:
    """Return True iff at least one non-invalidated API key with this
    name already exists. ES `get_api_key(name=...)` returns a list of
    matching keys; the bootstrap treats "any active match" as already-
    provisioned to keep the flow idempotent without needing to know
    which specific key was created on a prior run.
    """
    try:
        resp = es.security.get_api_key(name=name, owner=False)
    except es_exceptions.NotFoundError:
        return False
    keys = []
    if hasattr(resp, "get"):
        keys = resp.get("api_keys", [])
    return any(not k.get("invalidated", False) for k in keys)


def create_api_keys(es: Elasticsearch) -> dict[str, dict[str, Any]]:
    """Mint the `monitorul_reader` + `monitorul_indexer` API keys.

    Returns `{name: {id, api_key, encoded}}` for keys created during
    this call; already-present keys are absent from the dict (their
    plaintext is unrecoverable post-creation by design — the user
    must keep the original `es-init` output that minted them).
    """
    plan = (
        (API_KEY_READER, _reader_role_descriptor()),
        (API_KEY_INDEXER, _indexer_role_descriptor()),
    )
    created: dict[str, dict[str, Any]] = {}
    for name, descriptor in plan:
        if _api_key_exists(es, name):
            continue
        resp = es.security.create_api_key(
            name=name,
            role_descriptors={name: descriptor},
        )
        # Coerce the elastic-transport ObjectApiResponse into a plain
        # dict so the CLI layer doesn't have to import ES types just
        # to render the result.
        created[name] = {
            "id": resp["id"],
            "api_key": resp["api_key"],
            "encoded": resp["encoded"],
        }
    return created


def bootstrap(
    es: Elasticsearch, *, generation_suffix: str | None = None
) -> dict[str, Any]:
    """Run all four bootstrap steps in order. Returns a single result
    dict suitable for printing; individual helpers are exposed for
    callers that want finer control over what runs.
    """
    return {
        "component_templates": create_component_templates(es),
        "index_templates": create_index_templates(es),
        "indices": create_indices(es, generation_suffix=generation_suffix),
        "api_keys": create_api_keys(es),
    }


def smoke_roundtrip(es: Elasticsearch) -> bool:
    """Index one minimal document into `mo-documents` (via the read
    alias, which doubles as the search target) and read it back.

    The smoke uses `_id="mo://test/PII/0"` and refresh="wait_for" so
    the GET sees the doc without us racing the 30s refresh interval.
    Returns True on a successful round-trip; raises if anything goes
    wrong — bubbling the failure up keeps the CLI exit code honest.
    """
    test_id = "mo://test/PII/0"
    body = {
        "record_id": test_id,
        "document_id": test_id,
        "schema_version": "smoke",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "title": "Smoke test document",
        "year": 0,
    }
    es.index(
        index="mo-documents-write",
        id=test_id,
        document=body,
        refresh="wait_for",
    )
    got = es.get(index="mo-documents", id=test_id)
    return got["_id"] == test_id and got["_source"]["title"] == body["title"]
