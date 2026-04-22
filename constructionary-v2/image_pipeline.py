"""
Image pipeline: for each record in parts_records.jsonl, download each
image_url using the Promasch session cookies, upload to S3, and write the
S3 URL back into the record's image_s3_urls list.

S3 key format:  {S3_PART_IMAGE_PREFIX}/{entity_id}/{index}.{ext}

Resume-safe: skips images whose S3 key already exists (HEAD check).
Parallel with ThreadPoolExecutor (MAX_WORKERS).
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import config
from s3_images import (
    ext_for_content_type,
    guess_content_type,
    object_exists,
    part_image_key,
    s3_public_url,
    upload_image_bytes,
    _client,
)
from session_promasch import build_image_session, fetch_url_bytes
from utils import append_failed_image, load_jsonl, setup_logging

log = setup_logging("image_pipeline")


# ---------------------------------------------------------------------------
# Single-image worker
# ---------------------------------------------------------------------------

def _upload_one_image(
    url: str,
    s3_key: str,
    *,
    session,
    s3_client,
    dry_run: bool,
) -> Optional[str]:
    """Download url and upload to s3_key. Returns public URL or None on failure."""
    if object_exists(config.AWS_BUCKET_NAME, s3_key, s3_client):
        return s3_public_url(s3_key)

    body, err = fetch_url_bytes(
        session,
        url,
        timeout=config.IMAGE_DOWNLOAD_TIMEOUT,
        max_bytes=config.IMAGE_MAX_BYTES,
    )
    if body is None:
        raise RuntimeError(f"download failed: {err}")

    ct = guess_content_type(url, body)
    if not ct.startswith("image/"):
        raise RuntimeError(f"unexpected content-type: {ct}")

    if dry_run:
        return f"DRY_RUN:{s3_key}"

    result = upload_image_bytes(body, s3_key, ct, client=s3_client)
    if result is None:
        raise RuntimeError("S3 upload returned None")
    return result


# ---------------------------------------------------------------------------
# Per-record image processing
# ---------------------------------------------------------------------------

def process_record_images(
    record: dict,
    *,
    session,
    s3_client,
    failed_path: Path,
    dry_run: bool = False,
) -> dict:
    """
    Download + upload all images for one record. Returns the record with
    image_s3_urls populated.
    """
    image_urls: list[str] = record.get("image_urls", [])
    if not image_urls:
        return record

    entity_id = str(record.get("entity_id", "unknown"))
    s3_urls: list[str] = list(record.get("image_s3_urls", []))
    already_done = len(s3_urls)

    for idx, url in enumerate(image_urls):
        if idx < already_done:
            continue  # already uploaded in a previous run

        # Derive extension from content-type heuristic (we need body for that,
        # but we use a URL-based guess first and override after download).
        ext_guess = "jpg"
        for suffix in (".png", ".gif", ".webp", ".jpeg"):
            if url.lower().split("?")[0].endswith(suffix):
                ext_guess = suffix.lstrip(".")
                break

        s3_key = part_image_key(entity_id, idx, ext_guess)

        try:
            s3_url = _upload_one_image(url, s3_key, session=session, s3_client=s3_client, dry_run=dry_run)
            if s3_url:
                s3_urls.append(s3_url)
        except Exception as e:
            log.warning(
                "[images] FAILED entity_id=%s idx=%d: %s — %s",
                entity_id, idx, url[:80], e,
            )
            append_failed_image(failed_path, entity_id, url, str(e))

    record = dict(record)
    record["image_s3_urls"] = s3_urls
    return record


# ---------------------------------------------------------------------------
# Rewrite parts_records.jsonl in-place after image upload
# ---------------------------------------------------------------------------

def _rewrite_records(records_path: Path, updated: dict[str, dict]) -> None:
    """Rewrite parts_records.jsonl with updated records (keyed by entity_ref)."""
    lines: list[str] = []
    if records_path.exists():
        for line in records_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = rec.get("entity_ref") or rec.get("display_name", "")
                if key in updated:
                    rec = updated[key]
                lines.append(json.dumps(rec, ensure_ascii=False))
            except json.JSONDecodeError:
                lines.append(line)
    with open(records_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_image_pipeline(
    *,
    data_dir: Path,
    workers: int = 4,
    limit: Optional[int] = None,
    dry_run: bool = False,
    resume: bool = True,
) -> int:
    """
    Download and upload images for all records.
    Returns count of records that had at least one image processed.
    """
    records_path = data_dir / "parts_records.jsonl"
    auth_state_path = data_dir / "auth_state.json"
    failed_path = data_dir / "failed_images.json"

    records = load_jsonl(records_path)
    if not records:
        log.warning("[images] parts_records.jsonl is empty — run parse phase first.")
        return 0

    if limit is not None:
        records = records[:limit]

    # Filter records that already have all images uploaded
    if resume:
        pending = [
            r for r in records
            if len(r.get("image_urls", [])) > len(r.get("image_s3_urls", []))
        ]
        skipped = len(records) - len(pending)
        if skipped:
            log.info("[images] Resume: %d records fully done, %d pending", skipped, len(pending))
        records = pending

    if not records:
        log.info("[images] All images already uploaded.")
        return 0

    log.info("[images] Processing %d records with %d worker(s)", len(records), workers)

    session = build_image_session(auth_state_path)
    s3_client = None if dry_run else _client()

    updated: dict[str, dict] = {}  # entity_ref → updated record
    total_processed = 0
    failed_records = 0

    def _process(record: dict) -> Optional[dict]:
        try:
            return process_record_images(
                record,
                session=session,
                s3_client=s3_client,
                failed_path=failed_path,
                dry_run=dry_run,
            )
        except Exception as e:
            dn = record.get("display_name", "?")[:50]
            log.warning("[images] Unhandled error for %s: %s", dn, e)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, r): r for r in records}
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                key = result.get("entity_ref") or result.get("display_name", "")
                if key:
                    updated[key] = result
                if result.get("image_s3_urls"):
                    total_processed += 1
                    if total_processed % 50 == 0:
                        log.info("[images] %d records with images so far", total_processed)
            else:
                failed_records += 1

    # Rewrite JSONL with updated records
    if updated:
        log.info("[images] Writing %d updated records back to %s", len(updated), records_path)
        _rewrite_records(records_path, updated)

    log.info(
        "[images] Done: %d records with S3 images, %d failed.",
        total_processed, failed_records,
    )
    return total_processed
