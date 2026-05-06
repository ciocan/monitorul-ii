from __future__ import annotations

import pytest

from monitorul_ii.elasticsearch import ESConfig


_REQUIRED = ("ES_URL", "ES_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    """Strip ES_* vars so each test starts from a known-empty state.
    Mirrors the S3 test fixture pattern.
    """
    for k in (*_REQUIRED, "ES_VERIFY_CERTS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _set_required(monkeypatch):
    monkeypatch.setenv("ES_URL", "https://es.example.com:9200")
    monkeypatch.setenv("ES_API_KEY", "encoded-api-key")


def test_from_env_returns_config_when_all_set(clean_env):
    _set_required(clean_env)
    cfg = ESConfig.from_env()
    assert cfg is not None
    assert cfg.url == "https://es.example.com:9200"
    assert cfg.api_key == "encoded-api-key"
    # Default verify_certs is True — production-safe.
    assert cfg.verify_certs is True


@pytest.mark.parametrize("missing", _REQUIRED)
def test_from_env_returns_none_when_any_required_missing(clean_env, missing):
    _set_required(clean_env)
    clean_env.delenv(missing)
    assert ESConfig.from_env() is None


def test_from_env_returns_none_when_required_blank(clean_env):
    """Empty string is treated as missing — same contract as S3Config."""
    _set_required(clean_env)
    clean_env.setenv("ES_URL", "")
    assert ESConfig.from_env() is None


def test_from_env_verify_certs_false_via_zero(clean_env):
    _set_required(clean_env)
    clean_env.setenv("ES_VERIFY_CERTS", "0")
    cfg = ESConfig.from_env()
    assert cfg is not None
    assert cfg.verify_certs is False


def test_from_env_verify_certs_false_via_false(clean_env):
    _set_required(clean_env)
    clean_env.setenv("ES_VERIFY_CERTS", "false")
    cfg = ESConfig.from_env()
    assert cfg is not None
    assert cfg.verify_certs is False


def test_from_env_verify_certs_true_when_unset(clean_env):
    """Empty / missing ES_VERIFY_CERTS keeps the secure default."""
    _set_required(clean_env)
    clean_env.setenv("ES_VERIFY_CERTS", "")
    cfg = ESConfig.from_env()
    assert cfg is not None
    assert cfg.verify_certs is True


def test_from_env_verify_certs_invalid_value_raises(clean_env):
    _set_required(clean_env)
    clean_env.setenv("ES_VERIFY_CERTS", "maybe")
    with pytest.raises(ValueError, match="ES_VERIFY_CERTS"):
        ESConfig.from_env()
