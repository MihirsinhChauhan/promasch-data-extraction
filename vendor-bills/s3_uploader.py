"""
S3 upload module using boto3.

Uploads PDFs to:
  s3://{bucket}/po/{bill_id}.pdf
  s3://{bucket}/wo/{bill_id}.pdf
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional

import boto3
from botocore.exceptions import ClientError

import config
from utils import setup_logging, save_failed_id, ProgressTracker

log = setup_logging("s3_uploader")


def _get_client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
        region_name=config.AWS_REGION,
    )


def upload_pdf(
    pdf_bytes: bytes,
    bill_id: int,
    order_type: str,
    client=None,
) -> Optional[str]:
    """
    Upload a single PDF to S3. Returns the S3 URL on success, None on failure.
    """
    client = client or _get_client()
    key = f"{order_type.lower()}/{bill_id}.pdf"

    try:
        client.upload_fileobj(
            BytesIO(pdf_bytes),
            config.AWS_BUCKET_NAME,
            key,
            ExtraArgs={"ContentType": "application/pdf"},
        )
        s3_url = f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}.amazonaws.com/{key}"
        return s3_url

    except ClientError as e:
        log.error("S3 upload failed for %s/%d: %s", order_type, bill_id, e)
        save_failed_id(config.FAILED_IDS_FILE, order_type, bill_id, f"S3: {e}")
        return None


def upload_batch(
    pdf_map: dict[int, bytes],
    order_type: str,
    max_workers: int | None = None,
) -> dict[int, str]:
    """
    Upload multiple PDFs in parallel. Returns {bill_id: s3_url} for successful uploads.
    """
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    results: dict[int, str] = {}
    tracker = ProgressTracker(len(pdf_map), label=f"{order_type} S3 Upload")
    client = _get_client()

    log.info("Uploading %d %s PDFs to S3 with %d workers", len(pdf_map), order_type, max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(upload_pdf, pdf_bytes, bid, order_type, client): bid
            for bid, pdf_bytes in pdf_map.items()
        }

        for future in as_completed(future_to_id):
            bill_id = future_to_id[future]
            try:
                s3_url = future.result()
                if s3_url:
                    results[bill_id] = s3_url
                    tracker.tick(success=True)
                else:
                    tracker.tick(success=False)
            except Exception as e:
                log.error("Unexpected S3 error for %s/%d: %s", order_type, bill_id, e)
                tracker.tick(success=False)

    tracker.close()
    log.info("Upload complete: %s", tracker.summary_line())
    return results


def check_existing_keys(order_type: str, bill_ids: list[int]) -> set[int]:
    """
    Check which bill_ids already have PDFs in S3.
    Used for resume capability — skip re-uploading.
    """
    client = _get_client()
    prefix = f"{order_type.lower()}/"
    existing = set()

    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.AWS_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                # Key format: "po/12345.pdf"
                filename = obj["Key"].split("/")[-1]
                if filename.endswith(".pdf"):
                    try:
                        existing.add(int(filename.replace(".pdf", "")))
                    except ValueError:
                        pass
    except ClientError as e:
        log.warning("Could not list S3 keys for prefix '%s': %s", prefix, e)

    return existing
