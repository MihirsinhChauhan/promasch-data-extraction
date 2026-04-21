"""
S3 upload module for indent PDFs.

Uploads to:
  s3://{bucket}/indent/{indent_id}.pdf
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import config
from utils import ProgressTracker, save_failed_id, setup_logging

log = setup_logging("s3_uploader")

_S3_PREFIX = "indent/"


def _get_client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def upload_pdf(pdf_bytes: bytes, indent_id: str, *, client=None) -> Optional[str]:
    client = client or _get_client()
    key = f"{_S3_PREFIX}{indent_id}.pdf"
    try:
        client.upload_fileobj(
            BytesIO(pdf_bytes),
            config.AWS_BUCKET_NAME,
            key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        return (
            f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}"
            f".amazonaws.com/{key}"
        )
    except ClientError as e:
        log.error("S3 upload failed for indent/%s: %s", indent_id, e)
        save_failed_id(config.FAILED_IDS_FILE, indent_id, f"S3: {e}")
        return None


def upload_batch(pdf_map: dict[str, bytes], *, max_workers: Optional[int] = None) -> dict[str, str]:
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    if not pdf_map:
        return {}

    tracker = ProgressTracker(len(pdf_map), label="Indent S3 Upload")
    results: dict[str, str] = {}
    client = _get_client()
    log.info("Uploading %d indent PDFs to S3 with %d workers", len(pdf_map), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(upload_pdf, pdf_bytes, indent_id, client=client): indent_id
            for indent_id, pdf_bytes in pdf_map.items()
        }
        for future in as_completed(future_to_id):
            indent_id = future_to_id[future]
            try:
                s3_url = future.result()
                tracker.tick(success=bool(s3_url))
                if s3_url:
                    results[indent_id] = s3_url
            except Exception as e:
                log.error("Unexpected S3 error for indent/%s: %s", indent_id, e)
                tracker.tick(success=False)

    tracker.close()
    log.info("Upload complete: %s", tracker.summary_line())
    return results


def check_existing_keys(indent_ids: list[str]) -> set[str]:
    client = _get_client()
    existing: set[str] = set()

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=config.AWS_BUCKET_NAME,
            Prefix=_S3_PREFIX,
        ):
            for obj in page.get("Contents", []):
                filename = obj["Key"].split("/")[-1]
                if filename.endswith(".pdf"):
                    existing.add(filename[:-4])
    except ClientError as e:
        log.warning("Could not list S3 keys for prefix %r: %s", _S3_PREFIX, e)

    return existing.intersection(set(indent_ids))
