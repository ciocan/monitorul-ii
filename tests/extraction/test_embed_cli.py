"""Tests for the `monitorul-ii embed` argparse subcommand.

Mirrors the shape of `test_extract_cli.py`: parser-level assertions
(flags map to attributes, choices are validated) plus an end-to-end
dispatch test that mocks the embed service via `httpx.MockTransport`
and verifies the file system mutation + counter rollup.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from monitorul_ii import cli
from monitorul_ii.cli import _build_parser
from monitorul_ii.extraction.enrichments import embedding as emb


# ----------------------------------------------------------------------
# Parser-level assertions
# ----------------------------------------------------------------------


def test_embed_subcommand_in_parser():
    p = _build_parser()
    args = p.parse_args(["embed", "pdfs/"])
    assert args.command == "embed"
    assert args.paths == [Path("pdfs/")]


def test_embed_force_flag():
    p = _build_parser()
    args = p.parse_args(["embed", "x.json", "--force"])
    assert args.force is True


def test_embed_dry_run_flag():
    p = _build_parser()
    args = p.parse_args(["embed", "x.json", "--dry-run"])
    assert args.dry_run is True


def test_embed_url_flag():
    p = _build_parser()
    args = p.parse_args(["embed", "x.json", "--embed-url", "http://service:9000"])
    assert args.embed_url == "http://service:9000"


def test_embed_batch_size_flag():
    p = _build_parser()
    args = p.parse_args(["embed", "x.json", "--batch-size", "16"])
    assert args.batch_size == 16


def test_embed_default_url_is_none():
    """Default is None so cmd_embed picks up the env / module default."""
    p = _build_parser()
    args = p.parse_args(["embed", "x.json"])
    assert args.embed_url is None


def test_embed_inherits_s3_args():
    p = _build_parser()
    args = p.parse_args(["embed", "x.json", "--no-upload"])
    assert args.no_upload is True


# ----------------------------------------------------------------------
# Dispatch tests
# ----------------------------------------------------------------------


def _write_sidecar(tmp_path: Path, text: str) -> Path:
    """Write a minimal sidecar with one embeddable speech."""
    body = {
        "agenda_items": [
            {
                "id": "mo://2024/II/100#agenda-1",
                "title": "Discutarea proiectului de lege X" * 4,
                "ordinal": 1,
                "activities": [
                    {
                        "id": "mo://2024/II/100#agenda-1#act-1",
                        "type": "speech",
                        "text": text,
                        "speaker": {"name": "Ion Popescu"},
                    }
                ],
            }
        ]
    }
    p = tmp_path / "doc.extraction.json"
    p.write_text(
        json.dumps(
            {
                "document_id": "mo://2024/II/100",
                "content_sha": "sha-A",
                "body": body,
            }
        ),
        encoding="utf-8",
    )
    return p


def _stub_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path == "/healthz":
        return httpx.Response(
            200, json={"status": "ok", "model_id": "BAAI/bge-m3", "dims": 1024}
        )
    if request.method == "POST" and request.url.path == "/embed":
        payload = json.loads(request.content)
        n = len(payload["texts"])
        return httpx.Response(
            200,
            json={
                "vectors": [[0.001] * 1024 for _ in range(n)],
                "dims": 1024,
                "model_id": "BAAI/bge-m3",
            },
        )
    return httpx.Response(404)


class _Args:
    """argparse.Namespace stand-in for unit tests."""


def test_cmd_embed_no_files_returns_zero(tmp_path: Path, capsys):
    args = _Args()
    args.paths = [tmp_path]  # empty dir, no .extraction.json
    args.force = False
    args.dry_run = False
    args.embed_url = None
    args.batch_size = None
    args.no_upload = True
    args.bucket = None

    rc = cli.cmd_embed(args)
    assert rc == 0
    out = capsys.readouterr()
    assert "no .extraction.json" in out.err


def test_cmd_embed_dispatches_to_producer(tmp_path: Path, capsys):
    """End-to-end: a real sidecar gets walked, the stub service answers,
    the embedding file lands on disk with correct shape.
    """
    sidecar = _write_sidecar(
        tmp_path, "Aceasta este o intervenție lungă, peste pragul. " * 10
    )

    args = _Args()
    args.paths = [tmp_path]
    args.force = False
    args.dry_run = False
    args.embed_url = "http://stub-service"
    args.batch_size = None
    args.no_upload = True
    args.bucket = None

    transport = httpx.MockTransport(_stub_handler)
    real_client = httpx.Client

    def _patched_client(**kw):
        # The CLI builds its own client for the run; redirect via the
        # stub transport so no network call escapes the test.
        kw.pop("transport", None)
        return real_client(transport=transport, **kw)

    with patch.object(httpx, "Client", _patched_client):
        rc = cli.cmd_embed(args)

    assert rc == 0
    expected = sidecar.parent / emb.embedding_filename("doc")
    assert expected.exists()
    payload = json.loads(expected.read_text(encoding="utf-8"))
    # Two records: agenda title + speech text.
    assert "mo://2024/II/100#agenda-1" in payload
    assert "mo://2024/II/100#agenda-1#act-1" in payload
    speech = payload["mo://2024/II/100#agenda-1#act-1"]
    assert speech["_meta"]["producer"] == emb.EMBEDDING_PRODUCER
    assert len(speech["vector"]) == emb.EMBEDDING_DIMS

    out = capsys.readouterr()
    assert "embedded=" in out.out


def test_cmd_embed_health_failure_exits_2(tmp_path: Path, capsys):
    """If the service can't be reached, the CLI fails fast with exit 2."""

    sidecar = _write_sidecar(tmp_path, "long enough text " * 30)

    args = _Args()
    args.paths = [sidecar]
    args.force = False
    args.dry_run = False
    args.embed_url = "http://unreachable"
    args.batch_size = None
    args.no_upload = True
    args.bucket = None

    real_client = httpx.Client

    def _failing_client(**kw):
        kw.pop("transport", None)
        return real_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(503)),
            **kw,
        )

    with patch.object(httpx, "Client", _failing_client):
        rc = cli.cmd_embed(args)

    assert rc == 2
    err = capsys.readouterr().err
    assert "unreachable" in err


def test_cmd_embed_dry_run_skips_health_and_service(tmp_path: Path, capsys):
    """`--dry-run` short-circuits the health probe AND the embed call."""

    sidecar = _write_sidecar(tmp_path, "long enough text " * 30)

    args = _Args()
    args.paths = [sidecar]
    args.force = False
    args.dry_run = True
    args.embed_url = "http://stub-service"
    args.batch_size = None
    args.no_upload = True
    args.bucket = None

    call_count = {"n": 0}

    def _record(req):
        call_count["n"] += 1
        return httpx.Response(503)

    transport = httpx.MockTransport(_record)
    real_client = httpx.Client

    def _patched_client(**kw):
        kw.pop("transport", None)
        return real_client(transport=transport, **kw)

    with patch.object(httpx, "Client", _patched_client):
        rc = cli.cmd_embed(args)

    assert rc == 0
    # Neither healthz nor /embed was called.
    assert call_count["n"] == 0
    expected = sidecar.parent / emb.embedding_filename("doc")
    # Dry-run never writes.
    assert not expected.exists()
