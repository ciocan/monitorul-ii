"""Enrichment loader tests — file glob + version selection +
stale-fingerprint filter + journal merge.
"""

from __future__ import annotations

import json
from pathlib import Path


from monitorul_ii.elasticsearch import enrichments


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _basic_sidecar(tmp_path: Path, content_sha: str = "sha-current") -> Path:
    p = tmp_path / "doc.extraction.json"
    _write(p, {"document_id": "mo://X/Y/Z", "content_sha": content_sha})
    return p


# ----------------------------------------------------------------------
# list_enrichment_files / parse_enrichment_filename
# ----------------------------------------------------------------------


def test_list_enrichment_files_excludes_sidecar(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(tmp_path / "doc.topics.v0_1.json", {"r1": {"topics": ["x"]}})
    _write(tmp_path / "doc.embedding.v0_1.json", {"r1": {"embedding": [0.1]}})

    out = enrichments.list_enrichment_files(sidecar)
    names = sorted(p.name for p in out)
    assert names == ["doc.embedding.v0_1.json", "doc.topics.v0_1.json"]
    assert sidecar.name not in names


def test_list_enrichment_files_picks_up_journal(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    journal = tmp_path / "doc.journal.jsonl"
    journal.write_text('{"record_id":"r1","producer":"summarize"}\n', encoding="utf-8")

    out = enrichments.list_enrichment_files(sidecar)
    assert journal in out


def test_parse_enrichment_filename():
    assert enrichments.parse_enrichment_filename(Path("foo.topics.v0_1.json")) == (
        "topics",
        "0.1",
    )
    assert enrichments.parse_enrichment_filename(Path("foo.bge-m3.v1_2.json")) == (
        "bge-m3",
        "1.2",
    )
    assert enrichments.parse_enrichment_filename(Path("foo.journal.jsonl")) is None


# ----------------------------------------------------------------------
# fingerprint stability
# ----------------------------------------------------------------------


def test_enrichment_fingerprint_stable_across_runs(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(tmp_path / "doc.topics.v0_1.json", {"r1": {"topics": ["x"]}})

    a = enrichments.enrichment_fingerprint(sidecar)
    b = enrichments.enrichment_fingerprint(sidecar)
    assert a == b


def test_enrichment_fingerprint_changes_when_file_added(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(tmp_path / "doc.topics.v0_1.json", {"r1": {"topics": ["x"]}})
    a = enrichments.enrichment_fingerprint(sidecar)

    _write(tmp_path / "doc.embedding.v0_1.json", {"r1": {"embedding": [0.1]}})
    b = enrichments.enrichment_fingerprint(sidecar)
    assert a != b


def test_enrichment_fingerprint_no_files(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    fp = enrichments.enrichment_fingerprint(sidecar)
    assert fp  # non-empty


# ----------------------------------------------------------------------
# load_enrichments — version selection + stale filter
# ----------------------------------------------------------------------


def test_load_enrichments_collapses_single_key_payload(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(
        tmp_path / "doc.topics.v0_1.json",
        {
            "r1": {
                "_meta": {
                    "producer": "topics",
                    "version": "0.1",
                    "source_sidecar_content_sha": "sha-current",
                },
                "topics": ["just"],
            }
        },
    )
    result = enrichments.load_enrichments(sidecar, sidecar_content_sha="sha-current")
    assert "r1" in result
    assert "topics" in result["r1"]


def test_load_enrichments_picks_highest_version_by_default(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(
        tmp_path / "doc.topics.v0_1.json",
        {"r1": {"_meta": {"version": "0.1"}, "topics": ["v1-tag"]}},
    )
    _write(
        tmp_path / "doc.topics.v0_2.json",
        {"r1": {"_meta": {"version": "0.2"}, "topics": ["v2-tag"]}},
    )
    result = enrichments.load_enrichments(sidecar)
    assert result["r1"]["topics"]["topics"] == ["v2-tag"]


def test_load_enrichments_respects_live_versions(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    _write(
        tmp_path / "doc.topics.v0_1.json",
        {"r1": {"_meta": {"version": "0.1"}, "topics": ["v1-tag"]}},
    )
    _write(
        tmp_path / "doc.topics.v0_2.json",
        {"r1": {"_meta": {"version": "0.2"}, "topics": ["v2-tag"]}},
    )
    result = enrichments.load_enrichments(sidecar, live_versions={"topics": "0.1"})
    assert result["r1"]["topics"]["topics"] == ["v1-tag"]


def test_load_enrichments_stale_fingerprint_dropped(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path, content_sha="sha-current")
    _write(
        tmp_path / "doc.topics.v0_1.json",
        {
            "r1": {
                "_meta": {
                    "version": "0.1",
                    "source_sidecar_content_sha": "sha-OLD",
                },
                "topics": ["stale"],
            },
            "r2": {
                "_meta": {
                    "version": "0.1",
                    "source_sidecar_content_sha": "sha-current",
                },
                "topics": ["fresh"],
            },
        },
    )
    result = enrichments.load_enrichments(sidecar, sidecar_content_sha="sha-current")
    assert "r1" not in result
    assert "r2" in result


def test_load_enrichments_journal_appended(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    journal = tmp_path / "doc.journal.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "record_id": "r1",
                        "producer": "summarize",
                        "payload": {"text": "first"},
                    }
                ),
                json.dumps(
                    {
                        "record_id": "r1",
                        "producer": "summarize",
                        "payload": {"text": "second"},
                    }
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = enrichments.load_enrichments(sidecar)
    journal_payload = result["r1"]["journal"]["summarize"]
    assert len(journal_payload) == 2
    assert journal_payload[0]["text"] == "first"


def test_load_enrichments_no_files_returns_empty(tmp_path: Path):
    sidecar = _basic_sidecar(tmp_path)
    assert enrichments.load_enrichments(sidecar) == {}
