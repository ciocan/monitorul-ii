from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    access_key: str
    secret_key: str
    region: str
    bucket: str

    @classmethod
    def from_env(cls) -> S3Config | None:
        """Build config from S3_* env vars. Returns None if any required var is missing."""
        endpoint = os.environ.get("S3_ENDPOINT")
        access_key = os.environ.get("S3_ACCESS_KEY_ID")
        secret_key = os.environ.get("S3_SECRET_ACCESS_KEY")
        bucket = os.environ.get("S3_BUCKET")
        region = os.environ.get("S3_REGION") or "auto"
        if not (endpoint and access_key and secret_key and bucket):
            return None
        return cls(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            bucket=bucket,
        )


@dataclass(frozen=True)
class UploadResult:
    uploaded: bool
    etag: str | None


def _etag(head: dict) -> str | None:
    raw = head.get("ETag")
    return raw.strip('"') if raw else None


class Uploader:
    """Thin wrapper around boto3 for idempotent PDF uploads.

    Cloudflare R2 is S3-compatible — point S3_ENDPOINT at the R2 endpoint URL
    (`https://<account>.r2.cloudflarestorage.com`) and SigV4 just works.
    """

    def __init__(self, config: S3Config):
        self.config = config
        self._s3 = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
            config=BotoConfig(signature_version="s3v4"),
        )

    def validate(self) -> None:
        """head_bucket as a fail-fast check at startup."""
        self._s3.head_bucket(Bucket=self.config.bucket)

    def _head(self, key: str) -> dict | None:
        try:
            return self._s3.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as e:
            err = e.response.get("Error", {})
            if err.get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def exists(self, key: str) -> bool:
        return self._head(key) is not None

    def upload_if_missing(
        self,
        path: Path,
        key: str | None = None,
        content_type: str = "application/pdf",
        *,
        overwrite: bool = False,
    ) -> UploadResult:
        """Upload `path` to S3, optionally overwriting an existing object.

        Default (`overwrite=False`): skip the PUT when an object with `key`
        already exists. This is the right semantics for IMMUTABLE source
        artefacts — original PDFs and freshly-converted MDs whose bytes
        don't change after the first successful upload.

        `overwrite=True`: skip the head check and PUT unconditionally. Use
        this for MUTABLE artefacts whose bytes change in place after the
        initial write — sidecars rewritten by `extract` (extractor version
        bump), `link` (cross-doc / cross-reference fill), or `backfill`
        (registry-driven `*_normalized` / `Speaker.person_id` fill). The
        previous default-only behavior was a real bug: link / backfill
        modifications never made it to the bucket because the head_object
        gate short-circuited every re-upload, leaving the bucket carrying
        whatever `extract` last wrote.

        Returns UploadResult(uploaded, etag). `etag` is populated in both
        branches: from `head_object` when the object was already there
        (overwrite=False, key present), from a follow-up `head_object`
        after the upload otherwise. `uploaded=True` whenever a PUT
        actually happened — including the overwrite case.
        """
        object_key = key or path.name
        if not overwrite:
            head = self._head(object_key)
            if head is not None:
                return UploadResult(uploaded=False, etag=_etag(head))
        self._s3.upload_file(
            str(path),
            self.config.bucket,
            object_key,
            ExtraArgs={"ContentType": content_type},
        )
        head = self._s3.head_object(Bucket=self.config.bucket, Key=object_key)
        return UploadResult(uploaded=True, etag=_etag(head))
