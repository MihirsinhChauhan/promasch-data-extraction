"""Upload part images to S3 (vendor-bills pattern)."""

from __future__ import annotations

import mimetypes
from io import BytesIO
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import config


def _client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def guess_content_type(url: str, body: bytes) -> str:
    ct, _ = mimetypes.guess_type(url.split("?")[0])
    if ct and ct.startswith("image/"):
        return ct
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return "image/gif"
    if body.startswith(b"RIFF") and b"WEBP" in body[:12]:
        return "image/webp"
    return "application/octet-stream"


def s3_public_url(key: str) -> str:
    return f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}.amazonaws.com/{key}"


def object_exists(bucket: str, key: str, client=None) -> bool:
    client = client or _client()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def upload_image_bytes(
    body: bytes,
    key: str,
    content_type: str,
    *,
    client=None,
) -> Optional[str]:
    client = client or _client()
    try:
        client.upload_fileobj(
            BytesIO(body),
            config.AWS_BUCKET_NAME,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return s3_public_url(key)
    except ClientError:
        return None


def part_image_key(record_key: str, index: int, ext: str) -> str:
    ext = ext.lstrip(".") or "bin"
    return f"{config.S3_PART_IMAGE_PREFIX}/{record_key}/{index}.{ext}"


def ext_for_content_type(ct: str) -> str:
    if ct == "image/jpeg":
        return "jpg"
    if ct == "image/png":
        return "png"
    if ct == "image/gif":
        return "gif"
    if ct == "image/webp":
        return "webp"
    return "bin"
