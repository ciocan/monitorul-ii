"""CLI integration for `monitorul-ii index`.

The parser tests run argparse directly (no ES dependency); the handler
test drives `cmd_index` against a mocked indexer + mocked es_bulk so
the full command path is exercised without contacting a real cluster.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


from monitorul_ii import cli


def test_parser_accepts_index_with_defaults():
    p = cli._build_parser()
    args = p.parse_args(["index", "pdfs/"])
    assert args.command == "index"
    assert args.force is False
    assert args.dry_run is False
    assert args.target is None
    assert args.mirror is False
    assert args.rebuild is False
    assert args.grain is None
    assert args.index_generation == "live"
    assert args.reverse is False


def test_parser_accepts_reverse_flag():
    p = cli._build_parser()
    args = p.parse_args(["index", "pdfs/", "--reverse"])
    assert args.reverse is True


def test_cmd_index_reverse_walks_sidecars_newest_first(
    monkeypatch, tmp_path: Path, capsys
):
    """`--reverse` should flip the order in which `_index_one` is
    called — newest filename first, since names are date-prefixed.
    """
    monkeypatch.setenv("ES_URL", "https://es.example.com")
    monkeypatch.setenv("ES_API_KEY", "encoded")

    # Date-prefixed filenames so the natural sort is oldest→newest.
    names = [
        "2024-01-15_MO-PII-1-2024.extraction.json",
        "2024-06-20_MO-PII-200-2024.extraction.json",
        "2025-03-10_MO-PII-50-2025.extraction.json",
    ]
    paths = []
    for n in names:
        p = tmp_path / n
        p.write_text(json.dumps({"document_id": f"mo://X/Y/{n}"}), encoding="utf-8")
        paths.append(p)

    from monitorul_ii.elasticsearch.indexer import IndexResult

    seen: list[str] = []

    def fake_index_one(es, db, sidecar_path, **kwargs):
        seen.append(Path(sidecar_path).name)
        return IndexResult(
            document_id=f"mo://X/Y/{Path(sidecar_path).name}",
            action="indexed",
            grain_counts={"mo-documents": 1},
            child_record_ids=[],
        )

    with patch.object(cli, "_build_es_client") as build_es:
        build_es.return_value = object()
        with patch("monitorul_ii.elasticsearch.indexer.index_one", new=fake_index_one):
            parser = cli._build_parser()
            args = parser.parse_args(
                [
                    "index",
                    str(tmp_path),
                    "--reverse",
                    "--db",
                    str(tmp_path / "audit.db"),
                ]
            )
            rc = cli.cmd_index(args)

    assert rc == 0
    assert seen == list(reversed(names))
    capsys.readouterr()  # drain output


def test_parser_accepts_full_blue_green_flags():
    p = cli._build_parser()
    args = p.parse_args(
        [
            "index",
            "pdfs/",
            "--target",
            "mo-speeches-20260615-v2",
            "--mirror",
            "--grain",
            "mo-speeches",
            "--grain",
            "mo-documents",
            "--index-generation",
            "20260615-v2",
        ]
    )
    assert args.target == "mo-speeches-20260615-v2"
    assert args.mirror is True
    assert args.grain == ["mo-speeches", "mo-documents"]
    assert args.index_generation == "20260615-v2"


def test_parser_rebuild_requires_target_at_runtime(tmp_path: Path, capsys):
    """The argparser doesn't enforce the dependency (kept simple); the
    handler does. Drive cmd_index with `--rebuild` but no `--target`
    and assert the explicit error.
    """
    sidecar = tmp_path / "doc.extraction.json"
    sidecar.write_text(json.dumps({"document_id": "mo://X/Y/Z"}), encoding="utf-8")
    p = cli._build_parser()
    args = p.parse_args(["index", str(sidecar), "--rebuild"])
    rc = cli.cmd_index(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--rebuild requires --target" in err


def test_cmd_index_dry_run_does_not_need_es(tmp_path: Path, capsys):
    """Dry-run is the no-cluster preview; ES env vars need not be set."""
    sidecar = {
        "schema_version": "1.13.0",
        "document_id": "mo://2018/II/168",
        "content_sha": "sha-1",
        "document_type": "plenary_stenogram",
        "metadata": {
            "issue": "168",
            "year": 2018,
            "part": "II",
            "published": "2018-11-20",
            "chamber": "Camera Deputaților",
            "session_date": "2018-11-13",
        },
        "extraction": {"extractor_versions": {}},
        "coverage": {"claimed_pct": 0.99, "body_chars": 100},
        "body": {
            "session": {},
            "agenda_items": [],
            "interpellations": [],
        },
    }
    path = tmp_path / "doc.extraction.json"
    path.write_text(json.dumps(sidecar), encoding="utf-8")
    p = cli._build_parser()
    args = p.parse_args(
        [
            "index",
            str(path),
            "--dry-run",
            "--db",
            str(tmp_path / "audit.db"),
        ]
    )
    rc = cli.cmd_index(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry_run=1" in out


def test_cmd_index_no_sidecars_returns_zero(tmp_path: Path, capsys):
    """Empty input directory is a no-op success — same convention as
    other subcommands."""
    p = cli._build_parser()
    args = p.parse_args(
        ["index", str(tmp_path), "--dry-run", "--db", str(tmp_path / "audit.db")]
    )
    rc = cli.cmd_index(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "no .extraction.json files found" in err


def test_cmd_index_live_path_reports_indexed(monkeypatch, tmp_path: Path, capsys):
    """End-to-end CLI handler with mocked ES client + mocked
    `_index_one`. The handler should iterate, accumulate counters, and
    print the final summary.
    """
    monkeypatch.setenv("ES_URL", "https://es.example.com")
    monkeypatch.setenv("ES_API_KEY", "encoded")
    sidecar_path = tmp_path / "doc.extraction.json"
    sidecar_path.write_text(json.dumps({"document_id": "mo://X/Y/Z"}), encoding="utf-8")

    from monitorul_ii.elasticsearch.indexer import IndexResult

    def fake_index_one(*args, **kwargs):
        return IndexResult(
            document_id="mo://X/Y/Z",
            action="indexed",
            grain_counts={"mo-documents": 1},
            child_record_ids=["mo://X/Y/Z"],
        )

    with patch.object(cli, "_build_es_client") as build_es:
        build_es.return_value = object()
        # Patch the import-site name `index_one` used by cmd_index.
        with patch("monitorul_ii.elasticsearch.indexer.index_one", new=fake_index_one):
            p = cli._build_parser()
            args = p.parse_args(
                ["index", str(sidecar_path), "--db", str(tmp_path / "audit.db")]
            )
            rc = cli.cmd_index(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "indexed=1" in out
