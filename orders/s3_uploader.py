"""
S3 upload module for Orders PO PDFs.

Uploads PDFs to:
  s3://{bucket}/orders/po/{order_id}.pdf

Mirrors vendor-bills/s3_uploader.py.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import config
from utils import setup_logging, save_failed_id, ProgressTracker

log = setup_logging("s3_uploader")

_S3_PREFIX = "orders/po/"


def _get_client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def upload_pdf(
    pdf_bytes: bytes,
    order_id: str,
    *,
    client=None,
) -> Optional[str]:
    """Upload a single order PDF to S3.  Returns the S3 URL on success."""
    client = client or _get_client()
    key = f"{_S3_PREFIX}{order_id}.pdf"

    try:
        client.upload_fileobj(
            BytesIO(pdf_bytes),
            config.AWS_BUCKET_NAME,
            key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        s3_url = (
            f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}"
            f".amazonaws.com/{key}"
        )
        return s3_url

    except ClientError as e:
        log.error("S3 upload failed for order/%s: %s", order_id, e)
        save_failed_id(config.FAILED_IDS_FILE, order_id, f"S3: {e}")
        return None


def upload_batch(
    pdf_map: dict[str, bytes],
    *,
    max_workers: Optional[int] = None,
) -> dict[str, str]:
    """Upload multiple PDFs in parallel.  Returns {order_id: s3_url}."""
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    results: dict[str, str] = {}
    tracker = ProgressTracker(len(pdf_map), label="Orders S3 Upload")
    client = _get_client()

    log.info("Uploading %d order PDFs to S3 with %d workers", len(pdf_map), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(upload_pdf, pdf_bytes, oid, client=client): oid
            for oid, pdf_bytes in pdf_map.items()
        }

        for future in as_completed(future_to_id):
            order_id = future_to_id[future]
            try:
                s3_url = future.result()
                if s3_url:
                    results[order_id] = s3_url
                    tracker.tick(success=True)
                else:
                    tracker.tick(success=False)
            except Exception as e:
                log.error("Unexpected S3 error for order/%s: %s", order_id, e)
                tracker.tick(success=False)

    tracker.close()
    log.info("Upload complete: %s", tracker.summary_line())
    return results


def check_existing_keys(order_ids: list[str]) -> set[str]:
    """Return the subset of order_ids that already have PDFs in S3 (resume support)."""
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
                    existing.add(filename[:-4])   # strip .pdf
    except ClientError as e:
        log.warning("Could not list S3 keys for prefix %r: %s", _S3_PREFIX, e)

    return existing
