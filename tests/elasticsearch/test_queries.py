"""Reference-query layer tests.

Each of the 10 functions in `monitorul_ii.elasticsearch.queries` gets
at least one happy-path test plus one edge case (empty result, 404 on
missing record, paging beyond results, agg with no matches). The ES
client is a hand-rolled fake whose `search` and `get` methods return
fixture responses keyed by index — fast, deterministic, and lets us
also assert on the request body (filter shape, page size clamping,
sort order).
"""

from __future__ import annotations

from typing import Any

import pytest
from elasticsearch import exceptions as es_exceptions

from monitorul_ii.elasticsearch import queries


class FakeES:
    """A minimal ES stand-in that records every `search` / `get`
    invocation and returns a programmable response.

    `responses[index]` is the response dict; `not_found` is a set of
    `(index, id)` pairs that should raise `NotFoundError` on `get`.
    """

    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        not_found: set[tuple[str, str]] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.not_found = not_found or set()
        self.search_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def search(self, *, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.search_calls.append({"index": index, "body": body})
        return self.responses.get(
            index, {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        )

    def get(self, *, index: str, id: str) -> dict[str, Any]:  # noqa: A002
        self.get_calls.append({"index": index, "id": id})
        if (index, id) in self.not_found:
            raise es_exceptions.NotFoundError("404", meta=None, body={"found": False})
        # Allow tests to register a per-id `_source` via responses.
        key = f"{index}:{id}"
        if key in self.responses:
            return self.responses[key]
        return {"_id": id, "_source": {"id": id}}


def _hits_payload(
    hits: list[dict[str, Any]], total: int | None = None
) -> dict[str, Any]:
    return {
        "hits": {
            "total": {
                "value": total if total is not None else len(hits),
                "relation": "eq",
            },
            "hits": hits,
        }
    }


# ----------------------------------------------------------------------
# 1. search_speeches
# ----------------------------------------------------------------------


def test_search_speeches_happy_path_filters_and_query():
    es = FakeES(
        {
            queries.INDEX_SPEECHES: _hits_payload(
                [
                    {
                        "_id": "mo://2018/II/168#agenda-1#act-3",
                        "_score": 4.2,
                        "_source": {
                            "speaker": {"person_id": "iohannis-klaus"},
                            "text": "Doamnelor și domnilor...",
                            "is_substantive": True,
                        },
                    }
                ],
                total=1,
            )
        }
    )
    result = queries.search_speeches(
        es,
        q="educație",
        speaker_person_id="iohannis-klaus",
        chamber="Camera Deputaților",
        date_from="2018-01-01",
        date_to="2018-12-31",
        ref_bills=["L-100/2018"],
        topics=["educație"],
        page=2,
        page_size=10,
    )
    assert result.total == 1
    assert len(result.hits) == 1
    assert result.page == 2
    assert result.page_size == 10
    body = es.search_calls[0]["body"]
    assert body["from"] == 10
    assert body["size"] == 10
    must = body["query"]["bool"]["must"]
    filters = body["query"]["bool"]["filter"]
    # multi_match on q
    assert must[0]["multi_match"]["query"] == "educație"
    # All filters present
    flat = {next(iter(f.keys())): next(iter(f.values())) for f in filters}
    assert flat["term"] == {"is_substantive": True} or any(
        f.get("term") == {"speaker.person_id": "iohannis-klaus"} for f in filters
    )
    assert any(f.get("term") == {"chamber": "Camera Deputaților"} for f in filters)
    assert any(f.get("range", {}).get("session_date") for f in filters)
    assert any(f.get("terms") == {"refs.bills": ["L-100/2018"]} for f in filters)
    assert any(f.get("terms") == {"enrichments.topics": ["educație"]} for f in filters)


def test_search_speeches_clamps_page_size():
    es = FakeES({queries.INDEX_SPEECHES: _hits_payload([])})
    result = queries.search_speeches(es, page_size=999)
    assert result.page_size == queries.MAX_PAGE_SIZE
    body = es.search_calls[0]["body"]
    assert body["size"] == queries.MAX_PAGE_SIZE


def test_search_speeches_no_query_uses_match_all_and_date_sort():
    es = FakeES({queries.INDEX_SPEECHES: _hits_payload([])})
    queries.search_speeches(es)
    body = es.search_calls[0]["body"]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    # Date-first sort when no q is set; score-first when q is present.
    assert body["sort"][0] == {"session_date": "desc"}


def test_search_speeches_substantive_can_be_disabled():
    es = FakeES({queries.INDEX_SPEECHES: _hits_payload([])})
    queries.search_speeches(es, is_substantive=False)
    filters = es.search_calls[0]["body"]["query"]["bool"]["filter"]
    assert all(f != {"term": {"is_substantive": True}} for f in filters)


def test_search_speeches_empty_result():
    es = FakeES({queries.INDEX_SPEECHES: _hits_payload([], total=0)})
    result = queries.search_speeches(es, q="ничего нет")
    assert result.total == 0
    assert result.hits == []


# ----------------------------------------------------------------------
# 2. get_document
# ----------------------------------------------------------------------


def test_get_document_returns_source():
    es = FakeES(
        {
            f"{queries.INDEX_DOCUMENTS}:mo://2018/II/168": {
                "_id": "mo://2018/II/168",
                "_source": {
                    "document_id": "mo://2018/II/168",
                    "title": "Test",
                    "year": 2018,
                },
            }
        }
    )
    out = queries.get_document(es, "mo://2018/II/168")
    assert out is not None
    assert out["title"] == "Test"


def test_get_document_returns_none_on_404():
    es = FakeES(not_found={(queries.INDEX_DOCUMENTS, "mo://9999/II/0")})
    assert queries.get_document(es, "mo://9999/II/0") is None


# ----------------------------------------------------------------------
# 3. list_documents_by_date
# ----------------------------------------------------------------------


def test_list_documents_by_date_filters_chamber():
    es = FakeES({queries.INDEX_DOCUMENTS: _hits_payload([])})
    result = queries.list_documents_by_date(es, "2018-11-13", chamber="Senat")
    body = es.search_calls[0]["body"]
    filters = body["query"]["bool"]["filter"]
    assert {"term": {"session_date": "2018-11-13"}} in filters
    assert {"term": {"chamber": "Senat"}} in filters
    assert body["sort"] == [{"published": "desc"}]
    assert result.total == 0


def test_list_documents_by_date_no_chamber_skips_chamber_filter():
    es = FakeES({queries.INDEX_DOCUMENTS: _hits_payload([])})
    queries.list_documents_by_date(es, "2018-11-13")
    filters = es.search_calls[0]["body"]["query"]["bool"]["filter"]
    assert filters == [{"term": {"session_date": "2018-11-13"}}]


# ----------------------------------------------------------------------
# 4-5. get_agenda_item / get_speech
# ----------------------------------------------------------------------


def test_get_agenda_item_passes_id_through():
    es = FakeES(
        {
            f"{queries.INDEX_AGENDA_ITEMS}:mo://2018/II/168#agenda-1": {
                "_id": "mo://2018/II/168#agenda-1",
                "_source": {"ordinal": 1, "title": "Test"},
            }
        }
    )
    out = queries.get_agenda_item(es, "mo://2018/II/168#agenda-1")
    assert out is not None
    assert out["ordinal"] == 1


def test_get_speech_returns_none_when_below_substantive_cutoff():
    # Public read alias may genuinely lack the doc; behavior is identical
    # to a missing doc — both return None.
    es = FakeES(not_found={(queries.INDEX_SPEECHES, "mo://2018/II/168#agenda-1#act-1")})
    assert queries.get_speech(es, "mo://2018/II/168#agenda-1#act-1") is None


# ----------------------------------------------------------------------
# 6. person_page
# ----------------------------------------------------------------------


def test_person_page_assembles_person_recent_speeches_and_stats():
    person_source = {
        "id": "iordache-florin",
        "canonical_name": "Florin Iordache",
        "url_path": "/politicieni/iordache-florin",
    }
    speeches_response = {
        "hits": {
            "total": {"value": 42, "relation": "eq"},
            "hits": [
                {
                    "_id": "mo://2018/II/168#agenda-1#act-1",
                    "_score": 1.0,
                    "_source": {
                        "speaker": {"person_id": "iordache-florin"},
                        "text": "X" * 200,
                        "session_date": "2018-11-13",
                    },
                }
            ],
        },
        "aggregations": {
            "by_chamber": {"buckets": [{"key": "Camera Deputaților", "doc_count": 30}]},
            "by_year": {"buckets": [{"key": 2018, "doc_count": 12}]},
            "by_party": {"buckets": [{"key": "PSD", "doc_count": 30}]},
        },
    }
    es = FakeES(
        {
            f"{queries.INDEX_PERSONS}:iordache-florin": {
                "_id": "iordache-florin",
                "_source": person_source,
            },
            queries.INDEX_SPEECHES: speeches_response,
        }
    )
    page = queries.person_page(es, "iordache-florin")
    assert page is not None
    assert page.person == person_source
    assert len(page.recent_speeches) == 1
    assert page.stats["total_substantive_speeches"] == 42
    assert page.stats["by_chamber"][0]["key"] == "Camera Deputaților"
    # Speech filter scopes by person id
    body = es.search_calls[0]["body"]
    flt = body["query"]["bool"]["filter"]
    assert {"term": {"speaker.person_id": "iordache-florin"}} in flt


def test_person_page_returns_none_when_person_not_in_registry():
    es = FakeES(not_found={(queries.INDEX_PERSONS, "ghost-person")})
    assert queries.person_page(es, "ghost-person") is None


# ----------------------------------------------------------------------
# 7. search_persons
# ----------------------------------------------------------------------


def test_search_persons_runs_multimatch_over_canonical_and_aliases():
    es = FakeES({queries.INDEX_PERSONS: _hits_payload([], total=0)})
    queries.search_persons(es, "vacaroiu")
    body = es.search_calls[0]["body"]
    fields = body["query"]["multi_match"]["fields"]
    assert any(f.startswith("canonical_name") for f in fields)
    assert "aliases" in fields


def test_search_persons_uses_and_operator_for_multitoken_disambiguation():
    """Two-token name → both tokens required in the same field, else
    the long tail of every Nicolae floods the result. Production smoke
    on 2026-05-06 against the live cluster: `"vacaroiu nicolae"` with
    operator=or returned 386 hits; with operator=and returns just the
    handful of legitimate matches.
    """
    es = FakeES({queries.INDEX_PERSONS: _hits_payload([], total=0)})
    queries.search_persons(es, "vacaroiu nicolae")
    body = es.search_calls[0]["body"]
    assert body["query"]["multi_match"]["operator"] == "and"


def test_search_persons_empty_query_returns_empty_without_es_call():
    es = FakeES()
    result = queries.search_persons(es, "")
    assert result.total == 0
    assert result.hits == []
    assert es.search_calls == []


def test_search_persons_blank_whitespace_also_short_circuits():
    es = FakeES()
    queries.search_persons(es, "   ")
    assert es.search_calls == []


# ----------------------------------------------------------------------
# 8. list_committee_meetings
# ----------------------------------------------------------------------


def test_list_committee_meetings_filters_committee_id_and_date_range():
    es = FakeES({queries.INDEX_COMMITTEE_MEETINGS: _hits_payload([], total=0)})
    queries.list_committee_meetings(es, "comisia-juridica-cd", date_from="2024-01-01")
    body = es.search_calls[0]["body"]
    flt = body["query"]["bool"]["filter"]
    assert {"term": {"committee_id": "comisia-juridica-cd"}} in flt
    assert any(f.get("range", {}).get("meeting_date", {}).get("gte") for f in flt)
    assert body["sort"] == [{"meeting_date": "desc"}]


def test_list_committee_meetings_no_date_filter_when_unset():
    es = FakeES({queries.INDEX_COMMITTEE_MEETINGS: _hits_payload([])})
    queries.list_committee_meetings(es, "comisia-juridica-cd")
    flt = es.search_calls[0]["body"]["query"]["bool"]["filter"]
    assert flt == [{"term": {"committee_id": "comisia-juridica-cd"}}]


def test_list_committee_meetings_paging_beyond_results():
    es = FakeES({queries.INDEX_COMMITTEE_MEETINGS: _hits_payload([], total=0)})
    result = queries.list_committee_meetings(
        es, "comisia-juridica-cd", page=10, page_size=5
    )
    assert result.total == 0
    assert result.hits == []
    body = es.search_calls[0]["body"]
    assert body["from"] == 45
    assert body["size"] == 5


# ----------------------------------------------------------------------
# 9. get_report
# ----------------------------------------------------------------------


def test_get_report_returns_none_on_missing():
    es = FakeES(not_found={(queries.INDEX_REPORTS, "mo://2999/II/9999")})
    assert queries.get_report(es, "mo://2999/II/9999") is None


def test_get_report_returns_source_on_hit():
    es = FakeES(
        {
            f"{queries.INDEX_REPORTS}:mo://2014/II/100": {
                "_id": "mo://2014/II/100",
                "_source": {"issuing_body": "SRI"},
            }
        }
    )
    out = queries.get_report(es, "mo://2014/II/100")
    assert out is not None
    assert out["issuing_body"] == "SRI"


# ----------------------------------------------------------------------
# 10. agg_speeches_by_party_year
# ----------------------------------------------------------------------


def test_agg_speeches_by_party_year_returns_aggregations():
    es = FakeES(
        {
            queries.INDEX_SPEECHES: {
                "hits": {"total": {"value": 100, "relation": "eq"}, "hits": []},
                "aggregations": {
                    "by_party": {
                        "buckets": [
                            {
                                "key": "PSD",
                                "doc_count": 60,
                                "by_year": {
                                    "buckets": [{"key": 2018, "doc_count": 60}]
                                },
                                "speakers": {"value": 30},
                            }
                        ]
                    }
                },
            }
        }
    )
    result = queries.agg_speeches_by_party_year(es, year=2018)
    assert result.total == 100
    assert result.aggregations is not None
    assert result.aggregations["by_party"]["buckets"][0]["key"] == "PSD"
    body = es.search_calls[0]["body"]
    assert body["size"] == 0
    flt = body["query"]["bool"]["filter"]
    assert {"term": {"year": 2018}} in flt
    assert {"term": {"is_substantive": True}} in flt


def test_agg_speeches_by_party_year_empty_corpus():
    es = FakeES(
        {
            queries.INDEX_SPEECHES: {
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
                "aggregations": {"by_party": {"buckets": []}},
            }
        }
    )
    result = queries.agg_speeches_by_party_year(es)
    assert result.total == 0
    assert result.hits == []
    assert result.aggregations is not None
    assert result.aggregations["by_party"]["buckets"] == []


def test_agg_speeches_by_party_year_clamps_size():
    es = FakeES(
        {
            queries.INDEX_SPEECHES: {
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
                "aggregations": {"by_party": {"buckets": []}},
            }
        }
    )
    queries.agg_speeches_by_party_year(es, size=99999)
    by_party = es.search_calls[0]["body"]["aggs"]["by_party"]["terms"]
    assert by_party["size"] == 1000


# ----------------------------------------------------------------------
# Page-size clamp helper
# ----------------------------------------------------------------------


def test_clamp_page_size_floor_and_ceiling():
    assert queries._clamp_page_size(None) == queries.DEFAULT_PAGE_SIZE
    assert queries._clamp_page_size(0) == 1
    assert queries._clamp_page_size(-7) == 1
    assert queries._clamp_page_size(99999) == queries.MAX_PAGE_SIZE
    assert queries._clamp_page_size(25) == 25


# ----------------------------------------------------------------------
# Named queries dispatch table is in sync with the public API
# ----------------------------------------------------------------------


def test_named_queries_table_covers_all_public_functions():
    # The CLI relies on this dict; missing entries silently break the
    # `--name` switch.
    expected = {
        "search_speeches",
        "get_document",
        "list_documents_by_date",
        "get_agenda_item",
        "get_speech",
        "person_page",
        "search_persons",
        "list_committee_meetings",
        "get_report",
        "agg_speeches_by_party_year",
    }
    assert set(queries.NAMED_QUERIES) == expected
    for name, fn in queries.NAMED_QUERIES.items():
        assert callable(fn), f"{name} must be callable"


@pytest.mark.parametrize(
    "name",
    sorted(queries.NAMED_QUERIES),
)
def test_named_queries_match_module_attribute(name: str):
    assert queries.NAMED_QUERIES[name] is getattr(queries, name)
