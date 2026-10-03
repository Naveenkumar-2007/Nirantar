"""S3-compatible object storage for raw evidence. Content-addressed, tenant-prefixed keys:
s3://<bucket>/<tenant_id>/<modality>/<sha256>. Same bytes → same key (idempotent uploads)."""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Any

BUCKET = os.environ.get("EVIDENCE_BUCKET", "nirantar-evidence")


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3

    return boto3.client(
        "s3", endpoint_url=os.environ.get("S3_ENDPOINT", "http://localhost:8333"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "nirantar"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY", "nirantar-secret"), region_name="us-east-1")


def ensure_bucket() -> None:
    c = _client()
    existing = {b["Name"] for b in c.list_buckets().get("Buckets", [])}
    if BUCKET not in existing:
        c.create_bucket(Bucket=BUCKET)


def put(tenant_id: str, modality: str, data: bytes, content_type: str) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    key = f"{tenant_id}/{modality}/{sha}"
    ensure_bucket()
    _client().put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=content_type,
                         Metadata={"sha256": sha, "tenant": tenant_id})
    return f"s3://{BUCKET}/{key}", sha


def get(uri: str) -> bytes:
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    return bytes(_client().get_object(Bucket=bucket, Key=key)["Body"].read())
