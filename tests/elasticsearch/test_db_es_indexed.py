"""DB.es_indexed table behaviour — get/set/list + child_record_ids round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest

from monitorul_ii.db import DB


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "audit.db")


def test_get_indexed_state_none_for_unknown_doc(db: DB):
    assert db.get_indexed_state("mo://2018/II/168") is None


def test_set_then_get_round_trip(db: DB):
    db.set_indexed_state(
        "mo://2018/II/168",
        sidecar_content_sha="sha-1",
        enrichment_fingerprint="ef-1",
        index_generation="live",
        child_record_ids=["mo://2018/II/168", "mo://2018/II/168#agenda-1"],
    )
    state = db.get_indexed_state("mo://2018/II/168")
    assert state is not None
    assert state["sidecar_content_sha"] == "sha-1"
    assert state["enrichment_fingerprint"] == "ef-1"
    assert state["index_generation"] == "live"
    assert state["child_record_ids"] == [
        "mo://2018/II/168",
        "mo://2018/II/168#agenda-1",
    ]
    assert isinstance(state["indexed_at"], int)


def test_set_indexed_state_upsert(db: DB):
    db.set_indexed_state(
        "mo://2018/II/168",
        sidecar_content_sha="sha-1",
        enrichment_fingerprint="ef-1",
        index_generation="live",
        child_record_ids=["mo://2018/II/168"],
    )
    db.set_indexed_state(
        "mo://2018/II/168",
        sidecar_content_sha="sha-2",
        enrichment_fingerprint="ef-1",
        index_generation="live",
        child_record_ids=["mo://2018/II/168", "extra"],
    )
    state = db.get_indexed_state("mo://2018/II/168")
    assert state["sidecar_content_sha"] == "sha-2"
    assert "extra" in state["child_record_ids"]


def test_delete_indexed_state(db: DB):
    db.set_indexed_state(
        "mo://2018/II/168",
        sidecar_content_sha="sha-1",
        enrichment_fingerprint="ef-1",
        index_generation="live",
        child_record_ids=["mo://2018/II/168"],
    )
    db.delete_indexed_state("mo://2018/II/168")
    assert db.get_indexed_state("mo://2018/II/168") is None


def test_list_indexed_state_filter_by_generation(db: DB):
    for i in range(3):
        db.set_indexed_state(
            f"mo://X/Y/{i}",
            sidecar_content_sha=f"sha-{i}",
            enrichment_fingerprint="ef",
            index_generation="live" if i < 2 else "20260615-v2",
            child_record_ids=[],
        )
    live = db.list_indexed_state(generation="live")
    assert len(live) == 2
    assert all(r["index_generation"] == "live" for r in live)
    blue = db.list_indexed_state(generation="20260615-v2")
    assert len(blue) == 1
