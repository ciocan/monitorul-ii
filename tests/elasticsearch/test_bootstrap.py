"""Bootstrap helpers exercised against a mock Elasticsearch client.

We mock at the ES client surface — never hit a real cluster — but the
mock is concrete enough to assert the exact API calls each helper
makes. The acceptance gate at the CLI level (a live `monitorul-ii
es-init`) is the integration check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from elasticsearch import exceptions as es_exceptions

from monitorul_ii.elasticsearch import bootstrap


def _stub_client() -> MagicMock:
    """Build a MagicMock shaped like `elasticsearch.Elasticsearch`.

    We stub only the namespaces the bootstrap touches: cluster,
    indices, security, plus the top-level index/get for the smoke.
    """
    es = MagicMock()
    es.cluster = MagicMock()
    es.indices = MagicMock()
    es.security = MagicMock()
    return es


# --- create_component_templates -------------------------------------------


def test_create_component_templates_creates_both_when_absent():
    es = _stub_client()
    es.cluster.exists_component_template.return_value = False

    out = bootstrap.create_component_templates(es)

    assert {(e.name, e.created) for e in out} == {
        (bootstrap.COMPONENT_ANALYZERS, True),
        (bootstrap.COMPONENT_COMMON_FIELDS, True),
    }
    assert es.cluster.put_component_template.call_count == 2
    # Both calls go through with a `template` key (component-template shape).
    for call in es.cluster.put_component_template.call_args_list:
        kwargs = call.kwargs
        assert "template" in kwargs


def test_create_component_templates_skips_existing():
    es = _stub_client()
    es.cluster.exists_component_template.return_value = True

    out = bootstrap.create_component_templates(es)

    assert all(not e.created for e in out)
    es.cluster.put_component_template.assert_not_called()


# --- create_index_templates -----------------------------------------------


def test_create_index_templates_creates_one_per_grain_when_absent():
    es = _stub_client()
    es.indices.exists_index_template.return_value = False

    out = bootstrap.create_index_templates(es)

    assert len(out) == len(bootstrap.GRAINS)
    assert all(e.created for e in out)
    assert es.indices.put_index_template.call_count == len(bootstrap.GRAINS)
    # Names follow the `<grain>-template` pattern.
    names = {e.name for e in out}
    assert names == {f"{g}-template" for g in bootstrap.GRAINS}


def test_create_index_templates_skips_existing():
    es = _stub_client()
    es.indices.exists_index_template.return_value = True

    out = bootstrap.create_index_templates(es)

    assert all(not e.created for e in out)
    es.indices.put_index_template.assert_not_called()


# --- create_indices -------------------------------------------------------


def test_create_indices_creates_when_alias_absent_and_uses_suffix():
    es = _stub_client()
    es.indices.get_alias.side_effect = es_exceptions.NotFoundError(
        "alias not found", meta=MagicMock(), body={}
    )

    out = bootstrap.create_indices(es, generation_suffix="20260615-v1")

    assert len(out) == len(bootstrap.GRAINS)
    assert all(e.created for e in out)
    # Each create call carries both read + write aliases.
    for call in es.indices.create.call_args_list:
        kwargs = call.kwargs
        assert kwargs["index"].endswith("-20260615-v1")
        aliases = kwargs["aliases"]
        # Read alias is bare grain; write alias is `<grain>-write`
        # with is_write_index True.
        assert any(
            a.endswith("-write") and aliases[a].get("is_write_index") is True
            for a in aliases
        )
        assert any(not a.endswith("-write") for a in aliases)


def test_create_indices_skips_when_alias_already_exists():
    es = _stub_client()
    # Pretend each grain already has its read alias pointed at a live index.
    es.indices.get_alias.return_value = {"mo-documents-20250101-v1": {}}

    out = bootstrap.create_indices(es, generation_suffix="20260615-v1")

    assert len(out) == len(bootstrap.GRAINS)
    assert all(not e.created for e in out)
    es.indices.create.assert_not_called()


def test_generation_suffix_format():
    """Default suffix is `YYYYMMDD-v1` — the explicit override path is
    exercised in the create_indices test above; this one pins the
    shape of the auto-derived suffix.
    """
    suffix = bootstrap._generation_suffix()
    assert suffix.endswith("-v1")
    date_part = suffix[: -len("-v1")]
    assert len(date_part) == 8
    assert date_part.isdigit()


# --- create_api_keys ------------------------------------------------------


def test_create_api_keys_mints_both_when_absent():
    es = _stub_client()
    es.security.get_api_key.return_value = {"api_keys": []}
    es.security.create_api_key.side_effect = [
        {"id": "id-r", "api_key": "key-r", "encoded": "enc-r"},
        {"id": "id-i", "api_key": "key-i", "encoded": "enc-i"},
    ]

    out = bootstrap.create_api_keys(es)

    assert set(out) == {bootstrap.API_KEY_READER, bootstrap.API_KEY_INDEXER}
    # Reader gets read-only privileges; indexer gets write.
    reader_call = es.security.create_api_key.call_args_list[0]
    indexer_call = es.security.create_api_key.call_args_list[1]
    reader_descriptor = reader_call.kwargs["role_descriptors"][bootstrap.API_KEY_READER]
    indexer_descriptor = indexer_call.kwargs["role_descriptors"][
        bootstrap.API_KEY_INDEXER
    ]
    reader_privs = reader_descriptor["indices"][0]["privileges"]
    indexer_privs = indexer_descriptor["indices"][0]["privileges"]
    assert "write" not in reader_privs
    assert "read" in reader_privs
    assert "write" in indexer_privs


def test_create_api_keys_skips_existing_key():
    es = _stub_client()
    # First call: reader exists. Second call: indexer absent.
    es.security.get_api_key.side_effect = [
        {"api_keys": [{"name": bootstrap.API_KEY_READER, "invalidated": False}]},
        {"api_keys": []},
    ]
    es.security.create_api_key.return_value = {
        "id": "id-i",
        "api_key": "key-i",
        "encoded": "enc-i",
    }

    out = bootstrap.create_api_keys(es)

    # Only the indexer was created this run.
    assert set(out) == {bootstrap.API_KEY_INDEXER}
    assert es.security.create_api_key.call_count == 1


def test_create_api_keys_treats_invalidated_keys_as_absent():
    """Invalidated keys shouldn't count as 'already present' — the user
    invalidated them deliberately and is now re-bootstrapping.
    """
    es = _stub_client()
    es.security.get_api_key.return_value = {
        "api_keys": [
            {"name": bootstrap.API_KEY_READER, "invalidated": True},
        ]
    }
    es.security.create_api_key.return_value = {
        "id": "x",
        "api_key": "y",
        "encoded": "z",
    }

    out = bootstrap.create_api_keys(es)

    assert bootstrap.API_KEY_READER in out
    assert bootstrap.API_KEY_INDEXER in out


# --- bootstrap idempotency end-to-end -------------------------------------


def test_bootstrap_second_run_is_a_noop():
    """Run bootstrap twice against the same mock; the second run
    should hit zero `put_*` / `create_*` calls because every helper
    sees its targets already in place.
    """
    es = _stub_client()
    # First run: nothing exists yet.
    es.cluster.exists_component_template.return_value = False
    es.indices.exists_index_template.return_value = False
    es.indices.get_alias.side_effect = es_exceptions.NotFoundError(
        "missing", meta=MagicMock(), body={}
    )
    es.security.get_api_key.return_value = {"api_keys": []}
    es.security.create_api_key.return_value = {
        "id": "x",
        "api_key": "y",
        "encoded": "z",
    }

    bootstrap.bootstrap(es, generation_suffix="20260615-v1")

    # Now flip the world to "everything exists" and rerun.
    es.reset_mock()
    es.cluster.exists_component_template.return_value = True
    es.indices.exists_index_template.return_value = True
    es.indices.get_alias.side_effect = None
    es.indices.get_alias.return_value = {"mo-foo-20250101-v1": {}}
    es.security.get_api_key.return_value = {
        "api_keys": [
            {"name": bootstrap.API_KEY_READER, "invalidated": False},
            {"name": bootstrap.API_KEY_INDEXER, "invalidated": False},
        ]
    }

    bootstrap.bootstrap(es, generation_suffix="20260615-v1")

    es.cluster.put_component_template.assert_not_called()
    es.indices.put_index_template.assert_not_called()
    es.indices.create.assert_not_called()
    es.security.create_api_key.assert_not_called()


# --- smoke roundtrip ------------------------------------------------------


def test_smoke_roundtrip_returns_true_on_match():
    es = _stub_client()
    test_id = "mo://test/PII/0"
    es.get.return_value = {
        "_id": test_id,
        "_source": {"title": "Smoke test document"},
    }

    assert bootstrap.smoke_roundtrip(es) is True

    es.index.assert_called_once()
    es.get.assert_called_once()
    # Index call uses the write alias and the canonical record_id.
    index_kwargs = es.index.call_args.kwargs
    assert index_kwargs["index"] == "mo-documents-write"
    assert index_kwargs["id"] == test_id


def test_smoke_roundtrip_returns_false_on_mismatch():
    es = _stub_client()
    es.get.return_value = {
        "_id": "wrong",
        "_source": {"title": "Smoke test document"},
    }

    assert bootstrap.smoke_roundtrip(es) is False


# --- _resolve_existing_alias_target --------------------------------------


def test_resolve_existing_alias_target_returns_first_index():
    es = _stub_client()
    es.indices.get_alias.return_value = {
        "mo-documents-20250101-v1": {"aliases": {"mo-documents": {}}},
    }
    assert (
        bootstrap._resolve_existing_alias_target(es, "mo-documents")
        == "mo-documents-20250101-v1"
    )


def test_resolve_existing_alias_target_returns_none_on_404():
    es = _stub_client()
    es.indices.get_alias.side_effect = es_exceptions.NotFoundError(
        "missing", meta=MagicMock(), body={}
    )
    assert bootstrap._resolve_existing_alias_target(es, "mo-missing") is None


@pytest.mark.parametrize("name", [bootstrap.API_KEY_READER, bootstrap.API_KEY_INDEXER])
def test_role_descriptors_are_mo_scoped(name):
    """Each role descriptor must restrict to the `mo-*` index family —
    otherwise a leaked indexer key could touch unrelated indices on a
    multi-tenant cluster.
    """
    if name == bootstrap.API_KEY_READER:
        descriptor = bootstrap._reader_role_descriptor()
    else:
        descriptor = bootstrap._indexer_role_descriptor()
    assert descriptor["indices"][0]["names"] == ["mo-*"]


# --- update_live_mappings -------------------------------------------------


def test_update_live_mappings_calls_put_mapping_for_each_grain():
    """When every alias resolves to a live index, each grain's mapping
    JSON gets pushed via additive `put_mapping`.
    """
    es = _stub_client()
    es.indices.get_alias.side_effect = lambda name: {
        f"{name}-20260506-v1": {"aliases": {name: {}}}
    }

    out = bootstrap.update_live_mappings(es)

    assert len(out) == len(bootstrap.GRAINS)
    assert all(e.created for e in out)
    assert all(e.kind == "mapping" for e in out)
    assert es.indices.put_mapping.call_count == len(bootstrap.GRAINS)
    for call in es.indices.put_mapping.call_args_list:
        kwargs = call.kwargs
        assert kwargs["index"].endswith("-20260506-v1")
        assert "properties" in kwargs
        assert isinstance(kwargs["properties"], dict)


def test_update_live_mappings_skips_missing_aliases():
    """No alias → es-init hasn't been run for that grain → skip cleanly."""
    es = _stub_client()
    es.indices.get_alias.side_effect = es_exceptions.NotFoundError(
        "missing", meta=MagicMock(), body={}
    )

    out = bootstrap.update_live_mappings(es)

    assert len(out) == len(bootstrap.GRAINS)
    assert all(not e.created for e in out)
    es.indices.put_mapping.assert_not_called()


def test_update_live_mappings_includes_position_in_document():
    """Smoke: the additive field landed in this run is in the put_mapping
    body for every grain that should carry it (per the P4c follow-up).
    """
    es = _stub_client()
    es.indices.get_alias.side_effect = lambda name: {
        f"{name}-20260506-v1": {"aliases": {name: {}}}
    }

    bootstrap.update_live_mappings(es)

    grains_with_pid = {
        "mo-agenda-items",
        "mo-speeches",
        "mo-votes",
        "mo-interpellations",
        "mo-questions",
        "mo-committee-meetings",
    }
    for call in es.indices.put_mapping.call_args_list:
        kwargs = call.kwargs
        index_prefix = kwargs["index"].rsplit("-", 2)[0]
        if index_prefix in grains_with_pid:
            assert "position_in_document" in kwargs["properties"], (
                f"{index_prefix} mapping missing position_in_document field"
            )
