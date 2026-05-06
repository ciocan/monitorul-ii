"""CLI integration for `monitorul-ii es-init`.

Drives the parser + cmd_es_init handler with mocked elasticsearch
helpers — never reaches a live cluster. The dry-run path needs zero
mocks because it short-circuits before any client construction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from monitorul_ii import cli


# --- parser branch --------------------------------------------------------


def test_parser_accepts_es_init_with_dry_run():
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--dry-run"])
    assert args.command == "es-init"
    assert args.dry_run is True
    assert args.skip_smoke is False
    assert args.generation_suffix is None


def test_parser_accepts_explicit_generation_suffix():
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--generation-suffix", "20260615-v2"])
    assert args.generation_suffix == "20260615-v2"


# --- dry-run path ---------------------------------------------------------


def test_cmd_es_init_dry_run_does_not_touch_es(capsys):
    """Dry-run never builds an ES client; even with no env vars set it
    must succeed and emit the plan.
    """
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--dry-run", "--generation-suffix", "20260615-v1"])
    rc = cli.cmd_es_init(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "mo-documents-20260615-v1" in out
    # All nine grains appear.
    assert out.count("(aliases:") == 9
    assert "monitorul_reader" in out
    assert "monitorul_indexer" in out


# --- live path with mocked helpers ---------------------------------------


@pytest.fixture
def es_env(monkeypatch):
    monkeypatch.setenv("ES_URL", "https://es.example.com:9200")
    monkeypatch.setenv("ES_API_KEY", "encoded")
    monkeypatch.delenv("ES_VERIFY_CERTS", raising=False)


def _component_stub() -> list:
    from monitorul_ii.elasticsearch.bootstrap import (
        COMPONENT_ANALYZERS,
        COMPONENT_COMMON_FIELDS,
        CreatedEntity,
    )

    return [
        CreatedEntity(
            name=COMPONENT_ANALYZERS, kind="component_template", created=True
        ),
        CreatedEntity(
            name=COMPONENT_COMMON_FIELDS, kind="component_template", created=True
        ),
    ]


def _index_template_stub() -> list:
    from monitorul_ii.elasticsearch.bootstrap import GRAINS, CreatedEntity

    return [
        CreatedEntity(name=f"{g}-template", kind="index_template", created=True)
        for g in GRAINS
    ]


def _indices_stub() -> list:
    from monitorul_ii.elasticsearch.bootstrap import GRAINS, CreatedEntity

    return [
        CreatedEntity(
            name=f"{g}-20260615-v1",
            kind="index",
            created=True,
            detail=f"aliases: {g} (read), {g}-write (write)",
        )
        for g in GRAINS
    ]


def _api_keys_stub() -> dict:
    from monitorul_ii.elasticsearch.bootstrap import API_KEY_INDEXER, API_KEY_READER

    return {
        API_KEY_READER: {"id": "id-r", "api_key": "k-r", "encoded": "enc-r"},
        API_KEY_INDEXER: {"id": "id-i", "api_key": "k-i", "encoded": "enc-i"},
    }


def _patch_helpers(
    *,
    api_keys=None,
    api_key_exc=None,
    smoke_return=True,
    smoke_exc=None,
):
    """Patch every es_bootstrap helper cmd_es_init touches.

    The CLI now calls each helper individually so an api-key failure
    doesn't block the smoke; the test mock matches that decomposition.
    """
    if api_keys is None:
        api_keys = _api_keys_stub()
    return (
        patch("monitorul_ii.cli._build_es_client", return_value=MagicMock()),
        patch(
            "monitorul_ii.cli.es_bootstrap.create_component_templates",
            return_value=_component_stub(),
        ),
        patch(
            "monitorul_ii.cli.es_bootstrap.create_index_templates",
            return_value=_index_template_stub(),
        ),
        patch(
            "monitorul_ii.cli.es_bootstrap.create_indices",
            return_value=_indices_stub(),
        ),
        patch(
            "monitorul_ii.cli.es_bootstrap.create_api_keys",
            **(
                {"side_effect": api_key_exc}
                if api_key_exc is not None
                else {"return_value": api_keys}
            ),
        ),
        patch(
            "monitorul_ii.cli.es_bootstrap.smoke_roundtrip",
            **(
                {"side_effect": smoke_exc}
                if smoke_exc is not None
                else {"return_value": smoke_return}
            ),
        ),
    )


def test_cmd_es_init_live_path_runs_bootstrap_and_smoke(es_env, capsys):
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--generation-suffix", "20260615-v1"])

    patches = _patch_helpers()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5] as smoke,
    ):
        rc = cli.cmd_es_init(args)

    assert rc == 0
    out = capsys.readouterr().out
    # The encoded api keys are surfaced so the user can save them.
    assert "enc-r" in out
    assert "enc-i" in out
    assert "match: ok" in out
    smoke.assert_called_once()


def test_cmd_es_init_skip_smoke_skips_smoke_call(es_env, capsys):
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--skip-smoke"])

    patches = _patch_helpers()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5] as smoke,
    ):
        rc = cli.cmd_es_init(args)

    assert rc == 0
    smoke.assert_not_called()
    out = capsys.readouterr().out
    assert "smoke: skipped" in out


def test_cmd_es_init_returns_2_when_env_missing(monkeypatch, capsys):
    """Live path with no ES_URL / ES_API_KEY should refuse to proceed
    and exit with rc=2 (config error). Dry-run is the no-cluster
    escape hatch.
    """
    monkeypatch.delenv("ES_URL", raising=False)
    monkeypatch.delenv("ES_API_KEY", raising=False)

    p = cli._build_parser()
    args = p.parse_args(["es-init"])
    rc = cli.cmd_es_init(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "ES_URL" in err or "ES_API_KEY" in err


def test_cmd_es_init_smoke_mismatch_returns_1(es_env, capsys):
    """A smoke mismatch (round-trip didn't return what was indexed) is
    a real failure — exit 1, not silent success.
    """
    p = cli._build_parser()
    args = p.parse_args(["es-init"])

    patches = _patch_helpers(smoke_return=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        rc = cli.cmd_es_init(args)

    assert rc == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out


def test_cmd_es_init_skips_api_key_block_when_already_provisioned(es_env, capsys):
    """When create_api_keys returns no newly-created keys, the CLI prints
    the equals-marker line — never invents a fake encoded value.
    """
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--skip-smoke"])

    patches = _patch_helpers(api_keys={})
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        rc = cli.cmd_es_init(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "already provisioned" in out
    assert "enc-r" not in out


def test_cmd_es_init_api_key_failure_does_not_block_smoke(es_env, capsys):
    """An exception out of create_api_keys (e.g. derived bootstrap key)
    must NOT prevent the smoke round-trip from running — templates +
    indices are the load-bearing wiring.
    """
    p = cli._build_parser()
    args = p.parse_args(["es-init"])

    patches = _patch_helpers(api_key_exc=RuntimeError("derived api key"))
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5] as smoke,
    ):
        rc = cli.cmd_es_init(args)

    smoke.assert_called_once()
    # Templates + indices + smoke succeeded; api key step failed → rc=1.
    assert rc == 1
    captured = capsys.readouterr()
    assert "match: ok" in captured.out
    assert "NOT minted" in captured.err
    assert "derived api key" in captured.err


def test_cmd_es_init_api_key_failure_with_skip_smoke_returns_1(es_env, capsys):
    """Even with --skip-smoke, an api-key failure should surface as
    rc=1 so the operator notices.
    """
    p = cli._build_parser()
    args = p.parse_args(["es-init", "--skip-smoke"])

    patches = _patch_helpers(api_key_exc=RuntimeError("nope"))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        rc = cli.cmd_es_init(args)

    assert rc == 1
    err = capsys.readouterr().err
    assert "NOT minted" in err
