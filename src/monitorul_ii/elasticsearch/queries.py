"""Typed query layer over the `mo-*` Elasticsearch indices.

This module is the Python sister to the Next.js `lib/search.ts` query
layer described in Q9 of `docs/elasticsearch-indexing.md`. It is the
**only** path from app code (or the LLM-agent's tool wrappers) to ES.
Direct `es.search(...)` calls in callers are an anti-pattern — every
filter cap, paging cap, and aggregation guard belongs here, in one
place where the rules can be enforced.

Every function takes an `Elasticsearch` client as its first positional
argument (so callers can swap in mocks / `testcontainers` / a different
generation alias for blue-green smoke), reads through the per-grain
**read alias** (`mo-<grain>`, never the dated index directly — that's
the whole point of the alias), and returns a dataclass result whose
shape is stable across BM25 / RRF rank fusion (when P3 embeddings ship,
the `rank` field semantics change, the dataclass doesn't).

Design rules (mirrors Q9):

* **Page size hard cap = 50.** Public traffic and LLM-agent tools both
  enforce; the cap is a server-side correctness property, not a
  client-side ergonomic hint.
* **`is_substantive: true` default = True** on `mo-speeches`. The 100-
  char chair-procedure cutoff is documented in Q5; surfacing every
  "Mulțumesc, vă rog" turn floods every result page. Set False
  explicitly for the admin / discourse-research view.
* **`rank_fusion="bm25-only"` for v1.** RRF needs the `embedding`
  dense_vector field populated, which is P3 work; the param exists so
  callers can flip to `"rrf"` once embeddings are live without a
  signature change.
* **404 behavior**: lookup-by-id functions (`get_*`) return None; list
  / search / agg functions return empty result objects.
* **Aggregation cap** = 100 buckets per terms agg by default. ES
  cluster-level guard is 65536 (Q9); 100 keeps result payloads small
  and human-readable for the debug CLI.

If you find yourself writing a one-off ES query elsewhere in the code,
add a function here instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from elasticsearch import Elasticsearch
from elasticsearch import exceptions as es_exceptions

# Read aliases. NEVER call into a dated index directly — the alias is
# how blue-green cutover atomically swaps generations under load.
INDEX_DOCUMENTS = "mo-documents"
INDEX_AGENDA_ITEMS = "mo-agenda-items"
INDEX_SPEECHES = "mo-speeches"
INDEX_VOTES = "mo-votes"
INDEX_INTERPELLATIONS = "mo-interpellations"
INDEX_QUESTIONS = "mo-questions"
INDEX_COMMITTEE_MEETINGS = "mo-committee-meetings"
INDEX_REPORTS = "mo-reports"
INDEX_PERSONS = "mo-persons"

# Hard cap per Q9. Anything larger is either an LLM-agent abuse pattern
# (paging through speeches with the search endpoint when it should be
# scrolling enrichment files) or a UI bug.
MAX_PAGE_SIZE = 50

# Default page sizes — callers can override up to MAX_PAGE_SIZE.
DEFAULT_PAGE_SIZE = 20

# Terms-agg bucket cap. The cluster-level guard is 65536; 100 keeps
# `agg_speeches_by_party_year` results human-readable in the debug CLI
# and webapp UI. Callers needing the full distribution should iterate
# with composite aggs (not yet implemented; Q9 deferred).
DEFAULT_AGG_SIZE = 100

# Recent-speeches cap on `person_page` — the politician page shows the
# most-recent N speeches. The full archive is paged via search_speeches
# with `speakerPersonId` set.
PERSON_PAGE_RECENT_SPEECHES = 20


@dataclass
class SearchHit:
    """One hit row, normalised across grains.

    `score` is the BM25 relevance score (or RRF score once embeddings
    ship); `id` is the ES `_id` (= our canonical `record_id`); `source`
    is the unwrapped `_source` payload. `index` carries the per-hit
    `_index` value (`mo-speeches` etc.) so multi-index playback queries
    (`list_document_children`) can dispatch the renderer per grain
    without re-derived heuristics on the record_id pattern.
    """

    id: str
    score: float | None
    source: dict[str, Any]
    index: str | None = None


@dataclass
class SearchResult:
    """List-style result envelope used by `search_*` and `list_*`.

    `total` is the post-filter document count (ES hits.total.value);
    `page` and `page_size` echo the request so paging UIs can render
    "X-Y of Z" without re-deriving them. `aggregations` is the raw
    ES `aggregations` dict when the caller asked for any; None when
    the query carried no aggs.
    """

    total: int
    page: int
    page_size: int
    hits: list[SearchHit] = field(default_factory=list)
    aggregations: dict[str, Any] | None = None


@dataclass
class PersonPage:
    """Composite payload for `/politicieni/<slug>`.

    `person` is the `mo-persons` _source; `recent_speeches` is the
    PERSON_PAGE_RECENT_SPEECHES newest speeches by this person; `stats`
    is computed query-time from a `terms` agg over the matching speech
    set so the politician page never depends on the (Q4-deferred)
    pre-computed `mo-persons.stats` block.
    """

    person: dict[str, Any]
    recent_speeches: list[SearchHit] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


# ---------- internal helpers ----------


def _clamp_page_size(page_size: int | None) -> int:
    """Public-API guard. Clamp to [1, MAX_PAGE_SIZE] silently.

    Raising would surface as 500s in webapp and tool errors in the LLM
    agent; clamping is the documented contract (callers can introspect
    `result.page_size` to detect that they got less than asked).
    """
    if page_size is None:
        return DEFAULT_PAGE_SIZE
    if page_size < 1:
        return 1
    if page_size > MAX_PAGE_SIZE:
        return MAX_PAGE_SIZE
    return page_size


def _from(page: int, page_size: int) -> int:
    """1-indexed page → ES `from` offset."""
    if page < 1:
        page = 1
    return (page - 1) * page_size


def _hits_total(response: dict[str, Any]) -> int:
    """ES 8.x returns `hits.total` as `{value: int, relation: "eq"|"gte"}`."""
    raw = response.get("hits", {}).get("total")
    if isinstance(raw, dict):
        return int(raw.get("value", 0))
    if isinstance(raw, int):
        return raw
    return 0


_KNOWN_GRAINS: tuple[str, ...] = (
    INDEX_DOCUMENTS,
    INDEX_AGENDA_ITEMS,
    INDEX_SPEECHES,
    INDEX_VOTES,
    INDEX_INTERPELLATIONS,
    INDEX_QUESTIONS,
    INDEX_COMMITTEE_MEETINGS,
    INDEX_REPORTS,
    INDEX_PERSONS,
)


def _normalize_index(physical: str | None) -> str | None:
    """ES returns the underlying physical index name (`mo-speeches-
    20260506-v1`) on each hit, not the read alias. Map back to the
    logical grain name (`mo-speeches`) so callers can dispatch on a
    stable identifier across blue-green generation cuts.

    Falls through unchanged when no known grain prefix matches —
    forward-compat for indices added outside this module's
    knowledge.
    """
    if not physical:
        return physical
    for grain in _KNOWN_GRAINS:
        if physical == grain or physical.startswith(grain + "-"):
            return grain
    return physical


def _to_hits(response: dict[str, Any]) -> list[SearchHit]:
    return [
        SearchHit(
            id=h["_id"],
            score=h.get("_score"),
            source=h.get("_source", {}),
            index=_normalize_index(h.get("_index")),
        )
        for h in response.get("hits", {}).get("hits", [])
    ]


def _date_range_filter(
    field_name: str, *, gte: str | None, lte: str | None
) -> dict[str, Any] | None:
    """Build a `range` filter; return None when both bounds are unset."""
    if not gte and not lte:
        return None
    bounds: dict[str, Any] = {}
    if gte:
        bounds["gte"] = gte
    if lte:
        bounds["lte"] = lte
    return {"range": {field_name: bounds}}


def _safe_get(es: Elasticsearch, index: str, doc_id: str) -> dict[str, Any] | None:
    """`es.get(...)` with a NotFound catch returning None.

    The webapp / LLM-agent treats "no such record" as a 404 page, not
    an exception; centralise the catch so callers don't repeat it.
    """
    try:
        resp = es.get(index=index, id=doc_id)
    except es_exceptions.NotFoundError:
        return None
    return resp.get("_source")


# ---------- 1. search_speeches ----------


def search_speeches(
    es: Elasticsearch,
    *,
    q: str | None = None,
    speaker_person_id: str | None = None,
    chamber: str | None = None,
    document_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ref_bills: list[str] | None = None,
    topics: list[str] | None = None,
    is_substantive: bool = True,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    rank_fusion: str = "bm25-only",
) -> SearchResult:
    """Speech search — the substrate for the public search page and the
    LLM-agent's "find quotes by X about Y" tool.

    `q` runs as a `multi_match` over `text` + `agenda_title` +
    `speaker.name_search` (cross-field at default boosts; tune later
    once we have query logs). All non-text params apply as `term` /
    `terms` / `range` filters. `is_substantive=True` (default) hides
    the chair-procedure long tail per Q5; pass False for the admin
    / discourse-research view.

    `rank_fusion`:
      - `"bm25-only"` (default for v1): plain `_score` ranking.
      - `"rrf"`: the `_rrf` retriever over BM25 + kNN on
        `enrichments.embedding`. Requires P3 embeddings to be populated;
        falls back to BM25-only at runtime if the field is missing.
    """
    page_size = _clamp_page_size(page_size)
    must: list[dict[str, Any]] = []
    filters: list[dict[str, Any]] = []

    if q:
        must.append(
            {
                "multi_match": {
                    "query": q,
                    "fields": [
                        "text^2",
                        "agenda_title^1.5",
                        "speaker.name_search",
                    ],
                    "type": "best_fields",
                    "operator": "or",
                }
            }
        )

    if speaker_person_id:
        filters.append({"term": {"speaker.person_id": speaker_person_id}})
    if chamber:
        filters.append({"term": {"chamber": chamber}})
    if document_id:
        filters.append({"term": {"document_id": document_id}})
    if ref_bills:
        filters.append({"terms": {"refs.bills": ref_bills}})
    if topics:
        filters.append({"terms": {"enrichments.topics": topics}})
    if is_substantive:
        filters.append({"term": {"is_substantive": True}})

    rng = _date_range_filter("session_date", gte=date_from, lte=date_to)
    if rng:
        filters.append(rng)

    body: dict[str, Any] = {
        "from": _from(page, page_size),
        "size": page_size,
        "query": {
            "bool": {
                "must": must or [{"match_all": {}}],
                "filter": filters,
            }
        },
        "sort": (
            ["_score", {"session_date": "desc"}]
            if q
            else [{"session_date": "desc"}, "_score"]
        ),
        "track_total_hits": True,
    }

    # rank_fusion is intentionally a no-op until P3 embeddings ship.
    # Documenting the param now keeps the call sites stable across
    # the upcoming change; once `enrichments.embedding` is populated,
    # branch here on `rank_fusion == "rrf"` and emit a `retriever`
    # block instead of a `query` block.
    _ = rank_fusion

    response = es.search(index=INDEX_SPEECHES, body=body)
    return SearchResult(
        total=_hits_total(response),
        page=page,
        page_size=page_size,
        hits=_to_hits(response),
    )


# ---------- 1b. list_document_children ----------


# Default grain set when callers don't restrict — every per-doc child
# grain that carries a `position_in_document` field. `mo-documents` is
# the parent (no position); `mo-persons` is registry-driven (no doc
# affiliation); `mo-reports` has one record per R-suffix MO so source
# order isn't meaningful (`get_report` is the right helper there).
_PLAYBACK_GRAINS: tuple[str, ...] = (
    INDEX_AGENDA_ITEMS,
    INDEX_SPEECHES,
    INDEX_VOTES,
    INDEX_INTERPELLATIONS,
    INDEX_QUESTIONS,
    INDEX_COMMITTEE_MEETINGS,
)

# Cap for the multi-grain playback fetch. Plenary MOs run at most ~250
# child records (worst observed: 170 interpellations + ~80 speeches in
# `mo://2022/II/75`); 500 covers every doc with single-page headroom.
# Above that, callers should explicitly page via the `page` parameter.
PLAYBACK_PAGE_SIZE = 500


def list_document_children(
    es: Elasticsearch,
    document_id: str,
    *,
    grains: tuple[str, ...] | list[str] | None = None,
    page: int = 1,
    page_size: int = PLAYBACK_PAGE_SIZE,
) -> SearchResult:
    """Fetch every child record of one MO document, ordered by source
    position — the substrate for the `/mo/<id>` "full document
    playback" page.

    Returns a single `SearchResult` whose hits interleave grains
    (agenda items, speeches, votes, interpellations, questions,
    committee meetings) sorted by `position_in_document` ASC. Each
    hit's `_index` field tells the caller which grain it came from
    so the renderer can dispatch on type.

    Why a single multi-index query rather than per-grain calls + a
    client-side merge:

    1. **One ES round-trip.** The webapp's document page renders in
       the time of one bulk request, not 6 sequential ones.
    2. **Sort is server-side.** ES interleaves by `position_in_document`
       across indices for free; client-side merge would require
       carrying every grain's full result and re-sorting in Python.
    3. **Stable across mapping bumps.** When a future grain is added,
       extending `_PLAYBACK_GRAINS` is a one-line change; per-grain
       call sites would each need updating.

    `grains` restricts the fetch to a subset (e.g. `("mo-speeches",)`
    for speech-only playback). Default is every per-doc child grain.
    `page_size` defaults to 500 (covers every observed doc in one
    fetch); paging via `page` is available for outliers.

    Source-order sort key is `position_in_document` (the
    `source_span.chars[0]` denormalised by the indexer in v0.2.0+).
    Records without the field — e.g. ones written before the v0.2.0
    reindex — get sorted to the end via the `missing: "_last"` sort
    qualifier; the webapp can detect this by comparing returned IDs
    against the parent document's announced counts.
    """
    if not document_id:
        return SearchResult(total=0, page=page, page_size=page_size, hits=[])
    if page_size < 1:
        page_size = 1
    if page_size > PLAYBACK_PAGE_SIZE:
        page_size = PLAYBACK_PAGE_SIZE

    selected: tuple[str, ...]
    if grains:
        unknown = tuple(g for g in grains if g not in _PLAYBACK_GRAINS)
        if unknown:
            raise ValueError(
                f"unknown playback grain(s): {unknown}. choose from {_PLAYBACK_GRAINS}"
            )
        selected = tuple(grains)
    else:
        selected = _PLAYBACK_GRAINS

    body: dict[str, Any] = {
        "from": _from(page, page_size),
        "size": page_size,
        "query": {
            "bool": {
                "filter": [{"term": {"document_id": document_id}}],
            }
        },
        "sort": [
            {"position_in_document": {"order": "asc", "missing": "_last"}},
            # Tie-breaker: when two records share a span start (rare —
            # only when an extractor emits zero-width sub-records), use
            # record_id lex as a stable secondary key.
            {"record_id": "asc"},
        ],
        "track_total_hits": True,
    }
    response = es.search(index=",".join(selected), body=body)
    return SearchResult(
        total=_hits_total(response),
        page=page,
        page_size=page_size,
        hits=_to_hits(response),
    )


# ---------- 2. get_document ----------


def get_document(es: Elasticsearch, document_id: str) -> dict[str, Any] | None:
    """Look up one MO document by its canonical `document_id`
    (`mo://YYYY/PART/ISSUE`). Returns None when the doc isn't indexed.
    """
    return _safe_get(es, INDEX_DOCUMENTS, document_id)


# ---------- 3. list_documents_by_date ----------


def list_documents_by_date(
    es: Elasticsearch,
    date: str,
    chamber: str | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """List every MO document whose `session_date` matches `date`
    (single-day filter; pass `YYYY-MM-DD`). Optionally constrained
    to one chamber. Sorted by `published` descending.

    The webapp's `/calendar/<date>` page consumes this directly; the
    Next.js sitemap generation also uses it to enumerate per-day
    children for the per-day sitemap.xml shard.
    """
    page_size = _clamp_page_size(page_size)
    filters: list[dict[str, Any]] = [{"term": {"session_date": date}}]
    if chamber:
        filters.append({"term": {"chamber": chamber}})

    body = {
        "from": 0,
        "size": page_size,
        "query": {"bool": {"filter": filters}},
        "sort": [{"published": "desc"}],
        "track_total_hits": True,
    }
    response = es.search(index=INDEX_DOCUMENTS, body=body)
    return SearchResult(
        total=_hits_total(response),
        page=1,
        page_size=page_size,
        hits=_to_hits(response),
    )


# ---------- 4. get_agenda_item ----------


def get_agenda_item(es: Elasticsearch, record_id: str) -> dict[str, Any] | None:
    """Look up one agenda item by its canonical `record_id`
    (`mo://YYYY/PART/ISSUE#agenda-N`). Returns None when not indexed.
    """
    return _safe_get(es, INDEX_AGENDA_ITEMS, record_id)


# ---------- 5. get_speech ----------


def get_speech(es: Elasticsearch, record_id: str) -> dict[str, Any] | None:
    """Look up one speech / activity by its canonical `record_id`
    (`mo://YYYY/PART/ISSUE#agenda-N#act-M`). Returns None when not
    indexed (or when the speech is below the `is_substantive` cutoff
    on the public read alias).
    """
    return _safe_get(es, INDEX_SPEECHES, record_id)


# ---------- 6. person_page ----------


def person_page(es: Elasticsearch, person_slug: str) -> PersonPage | None:
    """Composite payload for `/politicieni/<slug>`: the person record,
    their N most-recent speeches, and a query-time stats rollup
    (chambers, year span, party-group histogram).

    Stats are computed from `terms` aggregations over `mo-speeches`
    rather than the (Q4-deferred) pre-computed `mo-persons.stats` block
    — that block is intentionally null on first-index per the design
    doc. Returns None when the person isn't in the registry.
    """
    person = _safe_get(es, INDEX_PERSONS, person_slug)
    if person is None:
        return None

    speech_body: dict[str, Any] = {
        "from": 0,
        "size": PERSON_PAGE_RECENT_SPEECHES,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"speaker.person_id": person_slug}},
                    {"term": {"is_substantive": True}},
                ]
            }
        },
        "sort": [{"session_date": "desc"}],
        "aggs": {
            "speech_count": {"value_count": {"field": "speaker.person_id"}},
            "by_chamber": {
                "terms": {"field": "chamber", "size": 8},
            },
            "by_year": {
                "terms": {"field": "year", "size": 50, "order": {"_key": "asc"}},
            },
            "by_party": {
                "terms": {
                    "field": "speaker.party_group_at_time",
                    "size": 30,
                    "missing": "(unknown)",
                },
            },
        },
        "track_total_hits": True,
    }
    response = es.search(index=INDEX_SPEECHES, body=speech_body)
    aggs = response.get("aggregations") or {}
    stats = {
        "total_substantive_speeches": _hits_total(response),
        "by_chamber": aggs.get("by_chamber", {}).get("buckets", []),
        "by_year": aggs.get("by_year", {}).get("buckets", []),
        "by_party_group_at_time": aggs.get("by_party", {}).get("buckets", []),
    }
    return PersonPage(
        person=person,
        recent_speeches=_to_hits(response),
        stats=stats,
    )


# ---------- 7. search_persons ----------


def search_persons(
    es: Elasticsearch,
    q: str,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """Free-text politician search, used by the global search bar's
    "people" facet and by the LLM-agent's "who is X" disambiguation
    tool.

    Searches `canonical_name` + `aliases` with a Romanian-folded text
    pass so diacritic-insensitive queries (`vacaroiu nicolae`) hit
    the canonical entry (`Văcăroiu Nicolae`). Empty `q` returns an
    empty result rather than the full registry — that's the sitemap's
    job, not the search bar's.

    `operator: "and"` is load-bearing for the disambiguation use case:
    when the user types a multi-token name like "vacaroiu nicolae",
    every token must appear in the SAME field (canonical_name or its
    folded variant) before a hit fires. Without `and`, the 2026-05
    smoke run showed `"vacaroiu nicolae"` returning 386 hits — the
    entire long tail of `Nicolae *` because `or` matches *either*
    token. Single-token queries (`"vacaroiu"`) still work, since
    `and` is a no-op on one term. Fuzzy matching on typos is
    deliberately not added — different concern, defer until query
    logs say it's needed.
    """
    page_size = _clamp_page_size(page_size)
    if not q or not q.strip():
        return SearchResult(total=0, page=page, page_size=page_size, hits=[])

    body: dict[str, Any] = {
        "from": _from(page, page_size),
        "size": page_size,
        "query": {
            "multi_match": {
                "query": q,
                "fields": [
                    "canonical_name^2",
                    "canonical_name.folded^1.5",
                    "aliases",
                ],
                "type": "best_fields",
                "operator": "and",
            }
        },
        "track_total_hits": True,
    }
    response = es.search(index=INDEX_PERSONS, body=body)
    return SearchResult(
        total=_hits_total(response),
        page=page,
        page_size=page_size,
        hits=_to_hits(response),
    )


# ---------- 8. list_committee_meetings ----------


def list_committee_meetings(
    es: Elasticsearch,
    committee_id: str,
    date_from: str | None = None,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> SearchResult:
    """List meetings for one committee, sorted newest first.

    Drives the `/comisie/<committee_id>` index page. `date_from`
    is the optional lower bound on `meeting_date` (used by the
    "this legislature" filter on the page).
    """
    page_size = _clamp_page_size(page_size)
    filters: list[dict[str, Any]] = [{"term": {"committee_id": committee_id}}]
    rng = _date_range_filter("meeting_date", gte=date_from, lte=None)
    if rng:
        filters.append(rng)

    body = {
        "from": _from(page, page_size),
        "size": page_size,
        "query": {"bool": {"filter": filters}},
        "sort": [{"meeting_date": "desc"}],
        "track_total_hits": True,
    }
    response = es.search(index=INDEX_COMMITTEE_MEETINGS, body=body)
    return SearchResult(
        total=_hits_total(response),
        page=page,
        page_size=page_size,
        hits=_to_hits(response),
    )


# ---------- 9. get_report ----------


def get_report(es: Elasticsearch, record_id: str) -> dict[str, Any] | None:
    """Look up one constitutional-body activity report by `record_id`
    (`mo://YYYY/II/ISSUE` for R-suffix reports — one report per MO).
    Returns None when not indexed.
    """
    return _safe_get(es, INDEX_REPORTS, record_id)


# ---------- 10. agg_speeches_by_party_year ----------


def agg_speeches_by_party_year(
    es: Elasticsearch,
    *,
    year: int | None = None,
    chamber: str | None = None,
    size: int = DEFAULT_AGG_SIZE,
) -> SearchResult:
    """Terms-agg health check + the discourse-analysis substrate.

    Returns total speeches and, in `aggregations`, a `by_party` terms
    bucket (party-group-at-time) with a `by_year` sub-aggregation —
    the substrate for "speeches per party per year" charts on the
    research dashboard. Optionally constrained to one year and / or
    chamber. Always filters to `is_substantive: true` (chair-procedure
    turns aren't useful in this view).
    """
    if size < 1:
        size = 1
    if size > 1000:
        size = 1000

    filters: list[dict[str, Any]] = [{"term": {"is_substantive": True}}]
    if year is not None:
        filters.append({"term": {"year": year}})
    if chamber:
        filters.append({"term": {"chamber": chamber}})

    body: dict[str, Any] = {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "by_party": {
                "terms": {
                    "field": "speaker.party_group_at_time",
                    "size": size,
                    "missing": "(unknown)",
                },
                "aggs": {
                    "by_year": {
                        "terms": {
                            "field": "year",
                            "size": 50,
                            "order": {"_key": "asc"},
                        }
                    },
                    "speakers": {
                        "cardinality": {"field": "speaker.person_id"},
                    },
                },
            },
        },
        "track_total_hits": True,
    }
    response = es.search(index=INDEX_SPEECHES, body=body)
    return SearchResult(
        total=_hits_total(response),
        page=1,
        page_size=0,
        hits=[],
        aggregations=response.get("aggregations"),
    )


# ---------- registry of named queries (debug CLI) ----------


# Maps a stable string name → callable. The CLI exposes this set as
# the `--name` option of `monitorul-ii query`. Adding a new query
# here is the documented way to extend the debug surface; never
# expose `es.search` directly.
NAMED_QUERIES: dict[str, Any] = {
    "search_speeches": search_speeches,
    "list_document_children": list_document_children,
    "get_document": get_document,
    "list_documents_by_date": list_documents_by_date,
    "get_agenda_item": get_agenda_item,
    "get_speech": get_speech,
    "person_page": person_page,
    "search_persons": search_persons,
    "list_committee_meetings": list_committee_meetings,
    "get_report": get_report,
    "agg_speeches_by_party_year": agg_speeches_by_party_year,
}


__all__ = [
    "INDEX_DOCUMENTS",
    "INDEX_AGENDA_ITEMS",
    "INDEX_SPEECHES",
    "INDEX_VOTES",
    "INDEX_INTERPELLATIONS",
    "INDEX_QUESTIONS",
    "INDEX_COMMITTEE_MEETINGS",
    "INDEX_REPORTS",
    "INDEX_PERSONS",
    "MAX_PAGE_SIZE",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_AGG_SIZE",
    "PERSON_PAGE_RECENT_SPEECHES",
    "SearchHit",
    "SearchResult",
    "PersonPage",
    "search_speeches",
    "list_document_children",
    "get_document",
    "list_documents_by_date",
    "get_agenda_item",
    "get_speech",
    "person_page",
    "search_persons",
    "list_committee_meetings",
    "get_report",
    "agg_speeches_by_party_year",
    "NAMED_QUERIES",
]
