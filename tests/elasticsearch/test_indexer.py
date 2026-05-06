"""Indexer tests — idempotency, force, orphan-delete, mirror, dry-run.

We mock `elasticsearch.helpers.bulk` and the `Elasticsearch` client at
the same surface the indexer uses; the test asserts on captured bulk
actions, delete-by-query bodies, and the SQLite state-row diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from monitorul_ii.db import DB
from monitorul_ii.elasticsearch import indexer


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _minimal_plenary(
    document_id: str = "mo://2018/II/168",
    *,
    content_sha: str = "sha-1",
    n_speeches: int = 2,
) -> dict[str, Any]:
    activities: list[dict[str, Any]] = []
    for i in range(1, n_speeches + 1):
        activities.append(
            {
                "id": f"{document_id}#agenda-1#act-{i}",
                "content_fingerprint": f"fp-act-{i}",
                "slug": f"act-{i}-tag",
                "type": "speech",
                "speaker": {
                    "raw": f"Speaker {i}",
                    "name": f"Speaker {i}",
                    "person_id": f"speaker-{i}",
                },
                "text": "X" * 200,
                "references_mentioned": [],
                "source_span": {"chars": [0, 200], "lines": [1, 5]},
            }
        )
    return {
        "schema_version": "1.13.0",
        "document_id": document_id,
        "content_sha": content_sha,
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": "168",
            "year": 2018,
            "part": "II",
            "published": "2018-11-20",
            "chamber": "Camera Deputaților",
            "session_date": "2018-11-13",
            "legislature": "VIII",
        },
        "extraction": {"extractor_versions": {}},
        "coverage": {"claimed_pct": 0.999, "body_chars": 1000},
        "body": {
            "session": {},
            "agenda_items": [
                {
                    "id": f"{document_id}#agenda-1",
                    "content_fingerprint": "fp-agenda-1",
                    "slug": "agenda-1-tag",
                    "ordinal": 1,
                    "title": "Test agenda",
                    "category": "other",
                    "outcome": "approved",
                    "primary_references": [],
                    "topics": {"primary": [], "secondary": []},
                    "activities": activities,
                    "source_span": {"chars": [0, 200], "lines": [1, 5]},
                }
            ],
            "interpellations": [],
        },
    }


def _write_sidecar(tmp_path: Path, sidecar: dict[str, Any]) -> Path:
    name = sidecar["document_id"].replace("mo://", "").replace("/", "-")
    path = tmp_path / f"{name}.extraction.json"
    path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path) -> DB:
    return DB(tmp_path / "audit.db")


# ----------------------------------------------------------------------
# Mock harness
# ----------------------------------------------------------------------


class _CapturingES:
    """Concrete ES-like stub we control end-to-end.

    Records calls to `delete_by_query` so the test can assert the body.
    Bulk goes through monkey-patched `es_bulk` — we capture there too.
    """

    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []
        self.deleted_count = 0

    def delete_by_query(
        self, *, index: str, refresh: bool, conflicts: str, **body: Any
    ):
        self.delete_calls.append({"index": index, "body": body})
        return {"deleted": self.deleted_count}


def _patch_bulk(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    """Replace `indexer.es_bulk` with a capturing fake. Returns a
    list whose entries are the actions list passed to each bulk call.
    """
    captured: list[list[dict[str, Any]]] = []

    def fake_bulk(es, actions, *_, **__):
        actions = list(actions)
        captured.append(actions)
        return len(actions), []

    monkeypatch.setattr(indexer, "es_bulk", fake_bulk)
    return captured


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_index_one_writes_state_and_returns_indexed(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    result = indexer.index_one(es, db, path)
    assert result.action == "indexed"
    # Plenary docs across documents/agenda/speeches/votes — votes is 0
    # for this sidecar (no vote activities), so 1+1+2 = 4 actions in
    # one bulk call.
    assert len(captured) == 1
    assert len(captured[0]) == 1 + 1 + 2  # documents + agenda-items + speeches
    state = db.get_indexed_state(sidecar["document_id"])
    assert state is not None
    assert state["sidecar_content_sha"] == "sha-1"
    assert sidecar["document_id"] in state["child_record_ids"]


def test_index_one_idempotent_second_run_skipped(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(es, db, path)
    captured.clear()

    second = indexer.index_one(es, db, path)
    assert second.action == "skipped"
    assert captured == []  # no bulk call


def test_index_one_force_reindexes(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(es, db, path)
    captured.clear()

    forced = indexer.index_one(es, db, path, force=True)
    assert forced.action == "indexed"
    assert len(captured) == 1


def test_orphan_delete_when_children_shrink(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary(n_speeches=3)
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(es, db, path)
    state = db.get_indexed_state(sidecar["document_id"])
    assert any("act-3" in rid for rid in state["child_record_ids"])

    # Re-extract: drop one speech. Bump content_sha so the idempotency
    # check skips.
    sidecar = _minimal_plenary(n_speeches=2, content_sha="sha-2")
    _write_sidecar(tmp_path, sidecar)
    captured.clear()
    es.deleted_count = 1

    result = indexer.index_one(es, db, path)
    assert result.action == "indexed"
    assert result.orphans_deleted == 1
    # delete_by_query was called against mo-speeches-write
    assert any(c["index"] == "mo-speeches-write" for c in es.delete_calls)
    body = es.delete_calls[0]["body"]
    # Filter scopes by document_id AND record_id terms
    filters = body["query"]["bool"]["filter"]
    doc_term = next(f for f in filters if "term" in f)
    assert doc_term["term"]["document_id"] == "mo://2018/II/168"
    rec_terms = next(f for f in filters if "terms" in f)
    assert "mo://2018/II/168#agenda-1#act-3" in rec_terms["terms"]["record_id"]


def test_dry_run_no_es_or_db_writes(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    result = indexer.index_one(es, db, path, dry_run=True)
    assert result.action == "dry-run"
    assert captured == []
    assert es.delete_calls == []
    assert db.get_indexed_state(sidecar["document_id"]) is None


def test_target_redirects_writes(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(
        es,
        db,
        path,
        target="mo-speeches-20260615-v2",
        index_generation="20260615-v2",
    )
    actions = captured[0]
    speech_actions = [a for a in actions if "speeches" in a["_index"]]
    assert all(a["_index"] == "mo-speeches-20260615-v2" for a in speech_actions)
    # Other grains still hit their live alias
    doc_actions = [a for a in actions if a["_index"] == "mo-documents-write"]
    assert len(doc_actions) == 1


def test_mirror_writes_to_both(tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(
        es,
        db,
        path,
        target="mo-speeches-20260615-v2",
        mirror=True,
    )
    actions = captured[0]
    speech_targets = {a["_index"] for a in actions if "speeches" in a["_index"]}
    assert speech_targets == {"mo-speeches-20260615-v2", "mo-speeches-write"}


def test_grain_filter_restricts_projection(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(es, db, path, grains=("mo-speeches",))
    actions = captured[0]
    assert all(a["_index"] == "mo-speeches-write" for a in actions)


def test_index_generation_state_tracked(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    sidecar = _minimal_plenary()
    path = _write_sidecar(tmp_path, sidecar)
    _patch_bulk(monkeypatch)
    es = _CapturingES()

    indexer.index_one(es, db, path, index_generation="live")
    state = db.get_indexed_state(sidecar["document_id"])
    assert state["index_generation"] == "live"

    # Run against a different generation — should NOT skip.
    second = indexer.index_one(
        es, db, path, target="mo-speeches-20260615-v2", index_generation="20260615-v2"
    )
    assert second.action == "indexed"
    state = db.get_indexed_state(sidecar["document_id"])
    assert state["index_generation"] == "20260615-v2"


def test_index_all_parallel_processes_every_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Parallel path runs through ThreadPoolExecutor with per-thread
    DB connections. We can't easily assert ordering (it's completion-
    order by design) but every sidecar must produce one IndexResult.
    """
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()
    db_path = tmp_path / "audit.db"

    paths = []
    for i in range(8):
        sidecar = _minimal_plenary(
            document_id=f"mo://2018/II/{100 + i}",
            content_sha=f"sha-{i}",
        )
        paths.append(_write_sidecar(tmp_path, sidecar))

    results = list(
        indexer.index_all_parallel(
            es, db_path, paths, workers=4, dry_run=False, force=False
        )
    )
    assert len(results) == 8
    assert {r.action for r in results} == {"indexed"}
    # Every sidecar's state row landed in the shared SQLite file.
    db = DB(db_path)
    try:
        rows = db.list_indexed_state()
        assert len(rows) == 8
    finally:
        db.close()
    # Each worker batched its own bulk call — at least one bulk per
    # sidecar (some ES bulk implementations may merge; we just check
    # the total action count).
    total_actions = sum(len(c) for c in captured)
    # 8 docs × (1 documents + 1 agenda-items + 2 speeches) = 32 actions
    assert total_actions == 8 * 4


def test_index_all_parallel_falls_back_to_sequential_for_workers_le_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`workers=1` short-circuits to the sequential generator path so
    callers don't pay pool-setup cost for trivial workloads.
    """
    _patch_bulk(monkeypatch)
    es = _CapturingES()
    db_path = tmp_path / "audit.db"
    sidecar = _minimal_plenary()
    paths = [_write_sidecar(tmp_path, sidecar)]

    results = list(
        indexer.index_all_parallel(es, db_path, paths, workers=1, dry_run=False)
    )
    assert len(results) == 1
    assert results[0].action == "indexed"


def test_index_persons_with_state_idempotent(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    """First call indexes; second call against the same registry
    payload skips via the sentinel state row.
    """
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()

    persons = {
        "version": "0.1.0",
        "entries": [
            {"id": "iordache-florin", "canonical_name": "Florin Iordache"},
            {"id": "vacaroiu-nicolae", "canonical_name": "Nicolae Văcăroiu"},
        ],
    }

    first = indexer.index_persons_with_state(es, db, persons)
    assert first.action == "indexed"
    assert first.grain_counts["mo-persons"] == 2
    assert "iordache-florin" in first.child_record_ids
    assert len(captured) == 1

    captured.clear()
    second = indexer.index_persons_with_state(es, db, persons)
    assert second.action == "skipped"
    assert captured == []


def test_index_persons_orphan_delete_on_registry_shrink(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    """A registry entry removed between runs is pulled from
    mo-persons via delete_by_query — exercises the orphan path
    on the registry projection.
    """
    _patch_bulk(monkeypatch)
    es = _CapturingES()
    es.deleted_count = 1

    big_registry = {
        "version": "0.1.0",
        "entries": [
            {"id": "a-x", "canonical_name": "A X"},
            {"id": "b-y", "canonical_name": "B Y"},
        ],
    }
    indexer.index_persons_with_state(es, db, big_registry)

    small_registry = {
        "version": "0.1.0",
        "entries": [{"id": "a-x", "canonical_name": "A X"}],
    }
    second = indexer.index_persons_with_state(es, db, small_registry)
    assert second.action == "indexed"
    assert second.orphans_deleted == 1
    assert any(c["index"] == "mo-persons-write" for c in es.delete_calls)


def test_index_persons_dry_run_no_es_no_state(
    tmp_path: Path, db: DB, monkeypatch: pytest.MonkeyPatch
):
    captured = _patch_bulk(monkeypatch)
    es = _CapturingES()
    persons = {"version": "0.1.0", "entries": [{"id": "x-y", "canonical_name": "X Y"}]}
    result = indexer.index_persons_with_state(es, db, persons, dry_run=True)
    assert result.action == "dry-run"
    assert captured == []
    assert db.get_indexed_state(indexer.PERSONS_STATE_KEY) is None


def test_index_all_parallel_single_path_short_circuit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """One sidecar + workers > 1 still uses sequential (no thread
    pool) — pool overhead would dwarf the per-task cost.
    """
    _patch_bulk(monkeypatch)
    es = _CapturingES()
    db_path = tmp_path / "audit.db"
    sidecar = _minimal_plenary()
    paths = [_write_sidecar(tmp_path, sidecar)]

    results = list(
        indexer.index_all_parallel(es, db_path, paths, workers=8, dry_run=False)
    )
    assert len(results) == 1
