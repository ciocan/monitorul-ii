from __future__ import annotations

import pytest

from monitorul_ii.uploader import S3Config, _etag


# --- S3Config.from_env -----------------------------------------------------


_REQUIRED = (
    "S3_ENDPOINT",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all S3_* vars so tests start from a known empty state."""
    for k in (*_REQUIRED, "S3_REGION"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _set_required(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "https://example.r2.cloudflarestorage.com")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "AKIA")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", "monitorul-ii")


def test_from_env_returns_config_when_all_set(clean_env):
    _set_required(clean_env)
    cfg = S3Config.from_env()
    assert cfg is not None
    assert cfg.endpoint == "https://example.r2.cloudflarestorage.com"
    assert cfg.access_key == "AKIA"
    assert cfg.secret_key == "secret"
    assert cfg.bucket == "monitorul-ii"
    # Region defaults to "auto" when not set.
    assert cfg.region == "auto"


def test_from_env_uses_explicit_region(clean_env):
    _set_required(clean_env)
    clean_env.setenv("S3_REGION", "us-east-1")
    cfg = S3Config.from_env()
    assert cfg is not None
    assert cfg.region == "us-east-1"


def test_from_env_returns_none_when_region_only_blank(clean_env):
    """Empty S3_REGION should fall back to 'auto', not propagate the blank."""
    _set_required(clean_env)
    clean_env.setenv("S3_REGION", "")
    cfg = S3Config.from_env()
    assert cfg is not None
    assert cfg.region == "auto"


@pytest.mark.parametrize("missing", _REQUIRED)
def test_from_env_returns_none_when_any_required_missing(clean_env, missing):
    _set_required(clean_env)
    clean_env.delenv(missing)
    assert S3Config.from_env() is None


def test_from_env_returns_none_when_required_blank(clean_env):
    """Empty string is treated as missing (S3 calls would fail anyway)."""
    _set_required(clean_env)
    clean_env.setenv("S3_BUCKET", "")
    assert S3Config.from_env() is None


# --- _etag -----------------------------------------------------------------


def test_etag_strips_quotes():
    assert _etag({"ETag": '"abc123"'}) == "abc123"


def test_etag_returns_none_when_missing():
    assert _etag({}) is None


def test_etag_handles_unquoted_value():
    """boto3 normally quotes ETags but the helper shouldn't trip if it ever doesn't."""
    assert _etag({"ETag": "abc123"}) == "abc123"
