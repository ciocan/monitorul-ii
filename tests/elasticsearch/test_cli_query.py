"""CLI integration for `monitorul-ii query`.

The parser tests exercise argparse directly; the handler tests drive
`cmd_query` against a fake `Elasticsearch` client built by patching
`_build_es_client`, so the full --name / --params / --explain path is
covered without contacting a real cluster.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from monitorul_ii import cli
from monitorul_ii.elasticsearch import queries


# ---------- parser tests ----------


def test_parser_accepts_query_with_minimum_args():
    p = cli._build_parser()
    args = p.parse_args(["query", "--name", "search_speeches"])
    assert args.command == "query"
    assert args.name == "search_speeches"
    assert args.params == "{}"
    assert args.explain is False


def test_parser_accepts_query_with_full_args():
    p = cli._build_parser()
    args = p.parse_args(
        [
            "query",
            "--name",
            "get_document",
            "--params",
            '{"document_id":"mo://2018/II/168"}',
            "--explain",
        ]
    )
    assert args.name == "get_document"
    assert args.params == '{"document_id":"mo://2018/II/168"}'
    assert args.explain is True


def test_parser_requires_name():
    p = cli._build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["query"])


# ---------- handler tests ----------


class _FakeES:
    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        get_payload: dict[str, Any] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.get_payload = get_payload
        self.search_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def search(self, *, index: str, body: dict[str, Any]):
        self.search_calls.append({"index": index, "body": body})
        return self.responses.get(
            index, {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
        )

    def get(self, *, index: str, id: str):  # noqa: A002
        self.get_calls.append({"index": index, "id": id})
        if self.get_payload is None:
            from elasticsearch import exceptions as es_exceptions

            raise es_exceptions.NotFoundError("404", meta=None, body={"found": False})
        return self.get_payload


@pytest.fixture
def es_env(monkeypatch: pytest.MonkeyPatch):
    """Provide ES_URL + ES_API_KEY so ESConfig.from_env() resolves."""
    monkeypatch.setenv("ES_URL", "https://localhost:9200")
    monkeypatch.setenv("ES_API_KEY", "fake-key")
    monkeypatch.delenv("ES_VERIFY_CERTS", raising=False)


def _drive_query(monkeypatch: pytest.MonkeyPatch, fake: _FakeES, argv: list[str]):
    """Run `cmd_query` end-to-end against a mocked client."""
    monkeypatch.setattr(cli, "_build_es_client", lambda cfg: fake)
    p = cli._build_parser()
    args = p.parse_args(argv)
    return cli.cmd_query(args)


def test_cmd_query_unknown_name_returns_2(es_env, monkeypatch, capsys):
    rc = _drive_query(monkeypatch, _FakeES(), ["query", "--name", "no_such_query"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown name" in err


def test_cmd_query_invalid_json_params_returns_2(es_env, monkeypatch, capsys):
    rc = _drive_query(
        monkeypatch,
        _FakeES(),
        ["query", "--name", "get_document", "--params", "{not json"],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err


def test_cmd_query_params_must_be_object(es_env, monkeypatch, capsys):
    rc = _drive_query(
        monkeypatch,
        _FakeES(),
        ["query", "--name", "search_speeches", "--params", "[1,2,3]"],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "JSON object" in err


def test_cmd_query_missing_required_positional(es_env, monkeypatch, capsys):
    """`get_document` requires `document_id` in --params."""
    rc = _drive_query(
        monkeypatch, _FakeES(), ["query", "--name", "get_document", "--params", "{}"]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires 'document_id'" in err


def test_cmd_query_missing_es_env_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("ES_URL", raising=False)
    monkeypatch.delenv("ES_API_KEY", raising=False)
    rc = _drive_query(monkeypatch, _FakeES(), ["query", "--name", "search_speeches"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "ES_URL" in err and "ES_API_KEY" in err


def test_cmd_query_search_speeches_happy_path(es_env, monkeypatch, capsys):
    fake = _FakeES(
        {
            queries.INDEX_SPEECHES: {
                "hits": {
                    "total": {"value": 1, "relation": "eq"},
                    "hits": [
                        {
                            "_id": "mo://2018/II/168#agenda-1#act-3",
                            "_score": 4.2,
                            "_source": {"text": "X" * 200},
                        }
                    ],
                }
            }
        }
    )
    rc = _drive_query(
        monkeypatch,
        fake,
        [
            "query",
            "--name",
            "search_speeches",
            "--params",
            '{"q":"educație","page_size":5}',
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["total"] == 1
    assert payload["page_size"] == 5
    assert len(payload["hits"]) == 1


def test_cmd_query_get_document_404_prints_null(es_env, monkeypatch, capsys):
    fake = _FakeES()  # no get_payload → NotFoundError
    rc = _drive_query(
        monkeypatch,
        fake,
        [
            "query",
            "--name",
            "get_document",
            "--params",
            '{"document_id":"mo://9999/II/0"}',
        ],
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "null"


def test_cmd_query_get_document_hit_prints_source(es_env, monkeypatch, capsys):
    fake = _FakeES(
        get_payload={
            "_id": "mo://2018/II/168",
            "_source": {"document_id": "mo://2018/II/168", "year": 2018},
        }
    )
    rc = _drive_query(
        monkeypatch,
        fake,
        [
            "query",
            "--name",
            "get_document",
            "--params",
            '{"document_id":"mo://2018/II/168"}',
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["year"] == 2018


def test_cmd_query_explain_traces_request_body(es_env, monkeypatch, capsys):
    fake = _FakeES(
        {
            queries.INDEX_SPEECHES: {
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
            }
        }
    )
    rc = _drive_query(
        monkeypatch,
        fake,
        ["query", "--name", "search_speeches", "--params", "{}", "--explain"],
    )
    assert rc == 0
    captured = capsys.readouterr()
    # The explain trace lands on stderr; the JSON result on stdout.
    assert "es.search" in captured.err
    # The result is still printed.
    assert json.loads(captured.out)["page_size"] == queries.DEFAULT_PAGE_SIZE


def test_cmd_query_bad_kwarg_returns_2(es_env, monkeypatch, capsys):
    """An unknown kwarg in --params should surface as a TypeError-as-2."""
    fake = _FakeES()
    rc = _drive_query(
        monkeypatch,
        fake,
        [
            "query",
            "--name",
            "search_speeches",
            "--params",
            '{"not_a_real_param":"x"}',
        ],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "bad parameters" in err


def test_cmd_query_es_error_returns_1(es_env, monkeypatch, capsys):
    """ES connection errors surface as exit 1, distinct from validation errors."""

    class ExplodingES(_FakeES):
        def search(self, **_kw):  # type: ignore[override]
            raise RuntimeError("connection refused")

    rc = _drive_query(
        monkeypatch,
        ExplodingES(),
        ["query", "--name", "search_speeches"],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "ES error" in err
