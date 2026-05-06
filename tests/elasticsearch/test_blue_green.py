"""Blue-green helper tests — atomic alias updates + safe drop."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from elasticsearch import exceptions as es_exceptions

from monitorul_ii.elasticsearch import blue_green


def _stub_es() -> MagicMock:
    es = MagicMock()
    es.indices = MagicMock()
    return es


def test_create_target_generation_creates_missing_indices():
    es = _stub_es()
    es.indices.exists.return_value = False

    created = blue_green.create_target_generation(
        es, ["mo-documents", "mo-speeches"], "20260615-v2"
    )

    assert created == [
        "mo-documents-20260615-v2",
        "mo-speeches-20260615-v2",
    ]
    assert es.indices.create.call_count == 2
    # Refresh interval is set to -1 for bulk-load freedom.
    for call in es.indices.create.call_args_list:
        settings = call.kwargs["settings"]
        assert settings["refresh_interval"] == "-1"


def test_create_target_generation_skips_existing():
    es = _stub_es()
    es.indices.exists.return_value = True

    created = blue_green.create_target_generation(es, ["mo-documents"], "20260615-v2")

    assert created == []
    es.indices.create.assert_not_called()


def test_create_target_generation_rejects_unknown_grain():
    es = _stub_es()
    with pytest.raises(ValueError):
        blue_green.create_target_generation(es, ["mo-bogus"], "20260615-v2")


def test_add_to_write_alias_demotes_existing_and_promotes_target():
    es = _stub_es()
    es.indices.get_alias.return_value = {
        "mo-speeches-20260101-v1": {},
    }

    blue_green.add_to_write_alias(es, "mo-speeches", "20260615-v2")

    actions = es.indices.update_aliases.call_args.kwargs["actions"]
    # Live target is demoted (is_write_index=False), target is promoted.
    assert actions[0]["add"]["index"] == "mo-speeches-20260101-v1"
    assert actions[0]["add"]["is_write_index"] is False
    assert actions[1]["add"]["index"] == "mo-speeches-20260615-v2"
    assert actions[1]["add"]["is_write_index"] is True


def test_add_to_write_alias_handles_missing_alias():
    es = _stub_es()
    es.indices.get_alias.side_effect = es_exceptions.NotFoundError("missing", {}, {})

    blue_green.add_to_write_alias(es, "mo-speeches", "20260615-v2")
    actions = es.indices.update_aliases.call_args.kwargs["actions"]
    assert actions == [
        {
            "add": {
                "index": "mo-speeches-20260615-v2",
                "alias": "mo-speeches-write",
                "is_write_index": True,
            }
        }
    ]


def test_swap_read_alias_atomic_update():
    es = _stub_es()
    es.indices.get_alias.return_value = {
        "mo-speeches-20260101-v1": {},
    }

    blue_green.swap_read_alias(es, "mo-speeches", "20260615-v2")

    actions = es.indices.update_aliases.call_args.kwargs["actions"]
    assert {
        "remove": {"index": "mo-speeches-20260101-v1", "alias": "mo-speeches"}
    } in actions
    assert {
        "add": {"index": "mo-speeches-20260615-v2", "alias": "mo-speeches"}
    } in actions


def test_drop_old_generation_deletes_when_present():
    es = _stub_es()
    es.indices.exists.return_value = True

    blue_green.drop_old_generation(es, "mo-speeches", "20260101-v1")
    es.indices.delete.assert_called_once_with(index="mo-speeches-20260101-v1")


def test_drop_old_generation_noop_when_missing():
    es = _stub_es()
    es.indices.exists.return_value = False

    blue_green.drop_old_generation(es, "mo-speeches", "20260101-v1")
    es.indices.delete.assert_not_called()


def test_list_generations_strips_prefix():
    es = _stub_es()
    es.indices.get.return_value = {
        "mo-speeches-20260101-v1": {},
        "mo-speeches-20260615-v2": {},
        "mo-other-foo": {},  # not this grain — must be excluded
    }

    out = blue_green.list_generations(es, "mo-speeches")
    assert out == ["20260101-v1", "20260615-v2"]
