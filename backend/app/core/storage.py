"""S3-compatible object storage (MinIO in dev).

Stores uploaded images / attachments / import files. boto3 is sync, so calls are
offloaded to a thread to stay non-blocking under the async server.
"""

from __future__ import annotations

import asyncio
import uuid
from functools import lru_cache

import boto3
from botocore.client import Config

from app.config import settings
from app.core.logging import get_logger

log = get_logger("storage")


@lru_cache
def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4"),
    )


def _ensure_bucket_sync() -> None:
    client = _client()
    try:
        client.head_bucket(Bucket=settings.minio_bucket)
    except Exception:
        try:
            client.create_bucket(Bucket=settings.minio_bucket)
        except Exception as exc:  # pragma: no cover
            log.warning("bucket_ensure_failed", error=str(exc))


async def ensure_bucket() -> None:
    await asyncio.to_thread(_ensure_bucket_sync)


def media_url(key: str) -> str:
    """Public URL for an object, served back through the app's /media proxy so it is
    reachable by the customer's browser (and the LLM) without exposing MinIO directly."""
    return f"{settings.app_base_url.rstrip('/')}/api/chat/media/{key}"


def _put_sync(key: str, data: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.minio_bucket, Key=key, Body=data, ContentType=content_type
    )


async def put_object(data: bytes, content_type: str, prefix: str = "uploads") -> str:
    """Store bytes and return the object key (use media_url(key) for the public URL)."""
    ext = _ext_for(content_type)
    key = f"{prefix}/{uuid.uuid4().hex}{ext}"
    await asyncio.to_thread(_put_sync, key, data, content_type)
    return key


def _get_sync(key: str) -> tuple[bytes, str]:
    obj = _client().get_object(Bucket=settings.minio_bucket, Key=key)
    return obj["Body"].read(), obj.get("ContentType", "application/octet-stream")


async def fetch_object(key: str) -> tuple[bytes, str]:
    """Read an object back (used by the /media proxy route)."""
    return await asyncio.to_thread(_get_sync, key)


# Upload policy shared by customer (/api/chat/upload) and operator (admin conversations) uploads.
ALLOWED_UPLOAD_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf", "text/plain", "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _delete_sync(keys: list[str]) -> int:
    deleted = 0
    for i in range(0, len(keys), 1000):  # S3 DeleteObjects caps at 1000 keys
        batch = [{"Key": k} for k in keys[i : i + 1000]]
        resp = _client().delete_objects(Bucket=settings.minio_bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch) - len(resp.get("Errors", []))
    return deleted


async def delete_objects(keys: list[str]) -> int:
    """Best-effort bulk delete (data retention / user deletion requests)."""
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return 0
    return await asyncio.to_thread(_delete_sync, keys)


def attachment_descriptor(key: str, name: str, content_type: str, size: int) -> dict:
    """The attachment shape stored on messages and pushed to clients."""
    return {
        "url": media_url(key),
        "key": key,
        "name": (name or "file")[:200],
        "content_type": content_type,
        "size": int(size),
        "kind": "image" if content_type.startswith("image/") else "file",
    }


def _ext_for(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "text/csv": ".csv",
        "application/json": ".json",
        "text/plain": ".txt",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(content_type, "")
