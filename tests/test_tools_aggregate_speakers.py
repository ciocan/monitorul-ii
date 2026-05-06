"""Tests for tools.aggregate_speakers — the speakers-raw + clusters
JSONL builder used to seed the persons.json long-tail resolution loop.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.aggregate_speakers import aggregate, _strip_honorific_from_raw


def _sidecar_with_speakers(
    doc_id: str, year: int, speakers: list[tuple[str, str | None]]
) -> dict:
    """Build a minimal sidecar shape carrying Speaker dicts under
    body.session.chair[*]. The aggregator walks any nesting; this is just
    the simplest shape to assert against.
    """
    chair = [
        {
            "raw": raw,
            "name": name,
            "title": None,
            "role": None,
            "party_group": None,
            "person_id": None,
        }
        for raw, name in speakers
    ]
    return {
        "document_id": doc_id,
        "metadata": {"year": year},
        "body": {
            "session": {
                "chair": chair,
                "chair_segments": [],
                "secretaries": [],
            }
        },
    }


def test_aggregate_collapses_identical_raws_across_docs(tmp_path: Path):
    """Two docs both carry `Domnul Florin Iordache` → 1 raw row, count=2."""
    s1 = tmp_path / "s1.extraction.json"
    s1.write_text(
        json.dumps(
            _sidecar_with_speakers(
                "mo://2018/II/1",
                2018,
                [("Domnul Florin Iordache", "Florin Iordache")],
            )
        ),
        encoding="utf-8",
    )
    s2 = tmp_path / "s2.extraction.json"
    s2.write_text(
        json.dumps(
            _sidecar_with_speakers(
                "mo://2019/II/1",
                2019,
                [("Domnul Florin Iordache", "Florin Iordache")],
            )
        ),
        encoding="utf-8",
    )

    raw_rows, cluster_rows = aggregate([s1, s2])
    assert len(raw_rows) == 1
    assert raw_rows[0]["raw"] == "Domnul Florin Iordache"
    assert raw_rows[0]["count"] == 2

    assert len(cluster_rows) == 1
    cl = cluster_rows[0]
    assert cl["normalized"] == "Florin Iordache"
    assert cl["total_count"] == 2
    assert cl["year_first"] == 2018
    assert cl["year_last"] == 2019


def test_aggregate_clusters_by_normalized_form(tmp_path: Path):
    """`Domnul Florin Iordache` and `Iordache Florin, deputat` cluster together."""
    s = tmp_path / "s.extraction.json"
    s.write_text(
        json.dumps(
            _sidecar_with_speakers(
                "mo://2018/II/1",
                2018,
                [
                    ("Domnul Florin Iordache", "Florin Iordache"),
                    (
                        "domnul deputat Florin Iordache, vicepreședinte al Camerei",
                        "Florin Iordache",
                    ),
                ],
            )
        ),
        encoding="utf-8",
    )

    raw_rows, cluster_rows = aggregate([s])
    assert len(raw_rows) == 2
    assert len(cluster_rows) == 1
    assert cluster_rows[0]["total_count"] == 2


def test_aggregate_skips_blank_raws(tmp_path: Path):
    s = tmp_path / "s.extraction.json"
    s.write_text(
        json.dumps(
            _sidecar_with_speakers(
                "mo://2018/II/1",
                2018,
                [("", None)],
            )
        ),
        encoding="utf-8",
    )
    raw_rows, cluster_rows = aggregate([s])
    assert raw_rows == []
    assert cluster_rows == []


def test_aggregate_handles_corrupt_sidecar(tmp_path: Path):
    bad = tmp_path / "bad.extraction.json"
    bad.write_text("{ not valid }", encoding="utf-8")
    good = tmp_path / "good.extraction.json"
    good.write_text(
        json.dumps(
            _sidecar_with_speakers(
                "mo://2018/II/1",
                2018,
                [("Domnul Klaus Iohannis", "Klaus Iohannis")],
            )
        ),
        encoding="utf-8",
    )
    raw_rows, cluster_rows = aggregate([bad, good])
    assert len(raw_rows) == 1
    assert raw_rows[0]["raw"] == "Domnul Klaus Iohannis"


def test_strip_honorific_helper():
    """The clustering helper peels honorific + title + role suffix."""
    assert _strip_honorific_from_raw("Domnul Florin Iordache") == "Florin Iordache"
    assert (
        _strip_honorific_from_raw(
            "domnul deputat Florin Iordache, vicepreședinte al Camerei"
        )
        == "Florin Iordache"
    )
    assert _strip_honorific_from_raw("Florin Iordache") == "Florin Iordache"
