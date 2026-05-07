"""Lock the v1 mapping JSONs.

These tests are not about *what* is indexed — that's locked at a higher
level by the design doc. They're about catching JSON-syntax errors and
shape regressions (a missing `index_patterns`, a typo in `composed_of`,
a dropped grain) before they reach a live cluster.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from monitorul_ii.elasticsearch import bootstrap


_MAPPING_PKG = "monitorul_ii.elasticsearch.mappings"


def _read_mapping(filename: str) -> dict:
    text = resources.files(_MAPPING_PKG).joinpath(filename).read_text(encoding="utf-8")
    return json.loads(text)


def test_analyzers_mapping_parses_and_contains_custom_analyzers():
    body = _read_mapping("_analyzers.json")
    analyzer = body["settings"]["analysis"]["analyzer"]
    assert "romanian_folded" in analyzer
    assert "romanian_exact" in analyzer
    # romanian_folded must include asciifolding for diacritic-insensitive matching.
    assert "asciifolding" in analyzer["romanian_folded"]["filter"]
    # romanian_exact must NOT include asciifolding — preserves diacritics.
    assert "asciifolding" not in analyzer["romanian_exact"]["filter"]


def test_common_fields_mapping_includes_record_id():
    body = _read_mapping("_common_fields.json")
    props = body["mappings"]["properties"]
    # record_id is the keystone — design doc Q2.
    assert props["record_id"] == {"type": "keyword"}
    assert props["document_id"] == {"type": "keyword"}
    assert props["content_fingerprint"] == {"type": "keyword"}


@pytest.mark.parametrize("grain", bootstrap.GRAINS)
def test_each_grain_mapping_parses_and_has_index_pattern(grain):
    body = _read_mapping(f"{grain}.json")
    # `index_patterns` is required for ES to know which concrete
    # indices the template applies to. The bootstrap relies on the
    # `<grain>-*` shape to match the dated generation indices.
    assert body["index_patterns"] == [f"{grain}-*"]
    assert "template" in body
    assert "mappings" in body["template"]


@pytest.mark.parametrize("grain", bootstrap.GRAINS)
def test_each_grain_mapping_composes_analyzers(grain):
    body = _read_mapping(f"{grain}.json")
    composed = body.get("composed_of", [])
    # Every grain references the analyzers component template — any
    # text field uses `romanian` (built-in) or one of our custom
    # analyzers, so the analyzer block must be in scope.
    assert bootstrap.COMPONENT_ANALYZERS in composed


@pytest.mark.parametrize(
    "grain",
    [g for g in bootstrap.GRAINS if g != "mo-persons"],
)
def test_non_persons_grains_compose_common_fields(grain):
    """Every grain except `mo-persons` carries `record_id` /
    `document_id` from the common-fields component template.
    `mo-persons` is registry-derived and uses `id` directly per Q1.
    """
    body = _read_mapping(f"{grain}.json")
    composed = body.get("composed_of", [])
    assert bootstrap.COMPONENT_COMMON_FIELDS in composed


def test_persons_grain_does_not_compose_common_fields():
    """mo-persons uses `id` (registry person_id) directly; it's not
    derived from a sidecar so `document_id` etc. don't apply.
    """
    body = _read_mapping("mo-persons.json")
    composed = body.get("composed_of", [])
    assert bootstrap.COMPONENT_COMMON_FIELDS not in composed


def test_speeches_mapping_has_dense_vector_and_substantive_filter():
    """Two design-doc invariants worth pinning:
    - `enrichments.embedding` is dense_vector / 1024 dim / cosine
    - `is_substantive` is a boolean (drives the SEO indexability cutoff)
    """
    body = _read_mapping("mo-speeches.json")
    props = body["template"]["mappings"]["properties"]
    embedding = props["enrichments"]["properties"]["embedding"]
    assert embedding["type"] == "dense_vector"
    assert embedding["dims"] == 1024
    assert embedding["similarity"] == "cosine"
    assert embedding["index"] is True
    assert props["is_substantive"]["type"] == "boolean"


def test_votes_mapping_has_defers_to_and_resolves_keywords():
    """defers_to / resolves are the linker-emitted fields; freezing
    them as keyword (not text) is intentional — they're document_id
    URI references, not searchable prose.
    """
    body = _read_mapping("mo-votes.json")
    props = body["template"]["mappings"]["properties"]
    assert props["defers_to"]["type"] == "keyword"
    assert props["resolves"]["type"] == "keyword"


def test_persons_mapping_has_nested_mandates_and_qid_keyword():
    body = _read_mapping("mo-persons.json")
    props = body["template"]["mappings"]["properties"]
    assert props["mandates"]["type"] == "nested"
    assert props["wikidata_qid"]["type"] == "keyword"


# Every user-searchable text field must carry a `.folded` subfield
# analyzed with `romanian_folded` (lowercase + asciifolding) so a query
# like `sosoaca` still matches indexed `șoșoacă`. The pairs below are
# the field paths exercised by the query layer's multi_match calls + the
# adjacent text fields a future query expansion is most likely to hit.
# `mo-persons.canonical_name.folded` is asserted by the persons-specific
# mapping fixture above; this list covers the other eight grains.
_FOLDED_FIELD_PATHS: list[tuple[str, list[str]]] = [
    ("mo-speeches", ["text", "agenda_title"]),
    ("mo-agenda-items", ["title"]),
    ("mo-interpellations", ["topic", "question_text", "response.text"]),
    ("mo-questions", ["topic", "text"]),
    ("mo-reports", ["title"]),
    ("mo-committee-meetings", ["committee_name", "purpose"]),
    ("mo-documents", ["title", "summary"]),
    ("mo-votes", ["agenda_title"]),
]


def _walk_field(props: dict, path: str) -> dict:
    """Walk a dotted field path through an ES mapping properties tree,
    descending into `properties` at each step. Errors on missing nodes.
    """
    node = props
    parts = path.split(".")
    for i, part in enumerate(parts):
        if "properties" in node:
            node = node["properties"]
        if part not in node:
            raise AssertionError(
                f"field path {path!r} missing at segment {part!r} (step {i})"
            )
        node = node[part]
    return node


@pytest.mark.parametrize(
    "grain,paths",
    _FOLDED_FIELD_PATHS,
    ids=[grain for grain, _ in _FOLDED_FIELD_PATHS],
)
def test_text_fields_carry_folded_subfield(grain, paths):
    body = _read_mapping(f"{grain}.json")
    props = body["template"]["mappings"]["properties"]
    for path in paths:
        node = _walk_field(props, path)
        assert node["type"] == "text", (
            f"{grain}.{path}: expected type=text, got {node!r}"
        )
        fields = node.get("fields", {})
        folded = fields.get("folded")
        assert folded is not None, f"{grain}.{path}: missing .folded subfield"
        assert folded == {"type": "text", "analyzer": "romanian_folded"}, (
            f"{grain}.{path}.folded: unexpected shape {folded!r}"
        )
