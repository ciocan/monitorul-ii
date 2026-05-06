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
