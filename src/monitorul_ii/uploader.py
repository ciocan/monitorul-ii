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

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except ClientError as e:
            err = e.response.get("Error", {})
            if err.get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def upload_if_missing(self, path: Path, key: str | None = None) -> bool:
        """Upload `path` to S3 unless an object with `key` already exists.

        Returns True when an upload happened, False when it was skipped.
        """
        object_key = key or path.name
        if self.exists(object_key):
            return False
        self._s3.upload_file(
            str(path),
            self.config.bucket,
            object_key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        return True
