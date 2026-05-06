from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from monitorul_ii.uploader import S3Config, Uploader, _etag


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


# --- Uploader.upload_if_missing -------------------------------------------


def _uploader_with_mock_client() -> tuple[Uploader, MagicMock]:
    """Build an Uploader whose boto3 client is a MagicMock — bypasses
    the real boto3 client constructor (which is exercised in the
    `validate` tests above) so we can assert exactly which S3 calls
    fire under each branch."""
    cfg = S3Config(
        endpoint="https://example.com",
        access_key="k",
        secret_key="s",
        region="auto",
        bucket="b",
    )
    up = Uploader.__new__(Uploader)
    up.config = cfg
    mock = MagicMock()
    up._s3 = mock
    return up, mock


def _not_found(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        op,
    )


def test_upload_if_missing_skips_when_object_exists(tmp_path: Path):
    """Default `overwrite=False`: if the bucket already has the key,
    return early without calling upload_file."""
    up, mock = _uploader_with_mock_client()
    p = tmp_path / "f.json"
    p.write_text("{}", encoding="utf-8")
    mock.head_object.return_value = {"ETag": '"existing-etag"'}

    result = up.upload_if_missing(p, content_type="application/json")

    assert result.uploaded is False
    assert result.etag == "existing-etag"
    mock.head_object.assert_called_once()  # the gate check
    mock.upload_file.assert_not_called()


def test_upload_if_missing_uploads_when_object_absent(tmp_path: Path):
    up, mock = _uploader_with_mock_client()
    p = tmp_path / "f.json"
    p.write_text("{}", encoding="utf-8")
    mock.head_object.side_effect = [
        _not_found("HeadObject"),
        {"ETag": '"new-etag"'},
    ]

    result = up.upload_if_missing(p, content_type="application/json")

    assert result.uploaded is True
    assert result.etag == "new-etag"
    mock.upload_file.assert_called_once()


def test_upload_if_missing_overwrite_true_skips_head_check_and_puts(
    tmp_path: Path,
):
    """`overwrite=True`: skip the pre-PUT head_object call and upload
    unconditionally — even when the bucket already has the key. This is
    the path link / backfill / extract take after rewriting a sidecar in
    place; the previous default-only behavior left the bucket carrying
    stale bytes."""
    up, mock = _uploader_with_mock_client()
    p = tmp_path / "f.json"
    p.write_text("{}", encoding="utf-8")
    # A single head_object call (the post-PUT one) returns the new etag.
    mock.head_object.return_value = {"ETag": '"new-etag"'}

    result = up.upload_if_missing(p, content_type="application/json", overwrite=True)

    assert result.uploaded is True
    assert result.etag == "new-etag"
    mock.upload_file.assert_called_once()
    # Exactly one head_object — the post-PUT one. Pre-PUT head is
    # skipped under overwrite=True.
    assert mock.head_object.call_count == 1


def test_upload_if_missing_overwrite_true_passes_content_type(
    tmp_path: Path,
):
    """ContentType propagates to the boto3 upload_file call regardless
    of the overwrite branch."""
    up, mock = _uploader_with_mock_client()
    p = tmp_path / "f.json"
    p.write_text("{}", encoding="utf-8")
    mock.head_object.return_value = {"ETag": '"x"'}

    up.upload_if_missing(p, content_type="application/json", overwrite=True)

    _, kwargs = mock.upload_file.call_args
    assert kwargs["ExtraArgs"]["ContentType"] == "application/json"
