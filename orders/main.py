#!/usr/bin/env python3
"""
Orders PO Extraction Pipeline
==============================

Phases:
  1. Scrape  — Playwright scrolls Orders > PO > Completed, extracts all PO entries
               via DOM text + GWT API interception + browser-side pagination.
               Writes data/po_catalog.json.

  2. Download — Parallel PDF downloads from OrderPdf endpoint.
               Uploads directly to S3.

  3. Merge   — Joins catalog with S3 URLs into data/final_output.json.

Usage:
  python main.py                          # Full pipeline
  python main.py --phase scrape           # Phase 1 only
  python main.py --phase download         # Phase 2 only (download + upload)
  python main.py --phase merge            # Phase 3 only
  python main.py --headful               # Show browser during scrape
  python main.py --skip-scrape           # Use existing po_catalog.json
  python main.py --limit 50              # Process only first 50 orders
  python main.py --dry-run               # Download + validate only, skip S3
  python main.py --retry-failed          # Re-process IDs from failed_ids.json
  python main.py --workers 5             # Override concurrency
  python main.py --order-ids 12345,12346 # Test specific order IDs
"""

from __future__ import annotations

import argparse
import sys

import config
from utils import (
    RunSummary,
    load_json,
    save_json,
    get_failed_order_ids,
    clear_failed_ids,
    setup_logging,
)

log = setup_logging("main")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_order_ids(s: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        if part not in seen:
            seen.add(part)
            out.append(part)
    return out


def _s3_url_for(order_id: str) -> str:
    return (
        f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}"
        f".amazonaws.com/orders/po/{order_id}.pdf"
    )


# ── Phases ───────────────────────────────────────────────────────────────────

def phase_scrape(headless: bool) -> list:
    from playwright_scraper import scrape_orders

    return scrape_orders(headless=headless)


def phase_download_and_upload(
    *,
    limit: int | None = None,
    dry_run: bool = False,
    retry_failed: bool = False,
    order_ids_override: list[str] | None = None,
    summary: RunSummary | None = None,
) -> dict[str, str]:
    """
    Download PDFs and upload to S3.
    Returns {order_id: s3_url} for every successfully uploaded PDF.
    """
    from pdf_downloader import download_batch
    from s3_uploader import upload_batch, check_existing_keys

    # Determine which IDs to process
    if retry_failed:
        remaining_ids = get_failed_order_ids(config.FAILED_IDS_FILE)
        if not remaining_ids:
            log.info("No failed IDs to retry")
            return {}
        log.info("Retrying %d previously failed IDs", len(remaining_ids))
        all_ids = list(remaining_ids)
    elif order_ids_override is not None:
        all_ids = list(order_ids_override)
        log.info("Using %d order ID(s) from --order-ids", len(all_ids))
    else:
        catalog = load_json(config.PO_CATALOG_FILE)
        if not catalog:
            log.warning(
                "po_catalog.json is empty. Run:\n"
                "  python main.py --phase scrape"
            )
            return {}
        all_ids = [e["order_id"] for e in catalog if e.get("order_id")]
        log.info("%d total order IDs from po_catalog.json", len(all_ids))

    # Resume: skip IDs already in S3
    s3_urls: dict[str, str] = {}
    if not dry_run:
        log.info("Checking S3 for existing PDFs ...")
        existing_in_s3 = check_existing_keys(all_ids)
        log.info(
            "%d already in S3, %d to process",
            len(existing_in_s3), len(all_ids) - len(existing_in_s3),
        )
        for oid in existing_in_s3:
            s3_urls[oid] = _s3_url_for(oid)
        remaining_ids = [oid for oid in all_ids if oid not in existing_in_s3]
    else:
        remaining_ids = list(all_ids)

    if limit is not None and len(remaining_ids) > limit:
        log.info("Applying --limit %d (from %d)", limit, len(remaining_ids))
        remaining_ids = remaining_ids[:limit]

    if not remaining_ids:
        log.info("Nothing to process — all IDs already uploaded")
        return s3_urls

    total_downloaded = 0
    total_uploaded = 0
    total_failed = 0
    batch_size = config.BATCH_SIZE

    for batch_start in range(0, len(remaining_ids), batch_size):
        batch = remaining_ids[batch_start: batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(remaining_ids) + batch_size - 1) // batch_size
        log.info(
            "Batch %d/%d: downloading %d PDFs",
            batch_num, total_batches, len(batch),
        )

        pdf_map = download_batch(batch)
        total_downloaded += len(pdf_map)
        total_failed += len(batch) - len(pdf_map)

        if pdf_map and not dry_run:
            batch_urls = upload_batch(pdf_map)
            s3_urls.update(batch_urls)
            total_uploaded += len(batch_urls)
            log.info(
                "Batch %d: %d downloaded, %d uploaded",
                batch_num, len(pdf_map), len(batch_urls),
            )
        elif dry_run and pdf_map:
            log.info(
                "Batch %d: %d downloaded + validated (dry-run, S3 skipped)",
                batch_num, len(pdf_map),
            )

    if summary:
        summary.add("PO Download",  total=len(remaining_ids), success=total_downloaded, failed=total_failed)
        if not dry_run:
            summary.add("PO S3 Upload", total=total_downloaded, success=total_uploaded, failed=total_downloaded - total_uploaded)

    return s3_urls


def phase_merge(s3_urls: dict[str, str] | None = None) -> list:
    """Join po_catalog.json with S3 URLs into final_output.json."""
    catalog = load_json(config.PO_CATALOG_FILE)
    if not catalog:
        log.warning("po_catalog.json is empty — merge skipped")
        return []

    existing_final = load_json(config.FINAL_OUTPUT_FILE)
    url_map: dict[str, str] = {
        r["order_id"]: r["s3_url"]
        for r in existing_final
        if r.get("order_id") and r.get("s3_url")
    }
    url_map.update(s3_urls or {})

    final_records = []
    for entry in catalog:
        order_id = entry.get("order_id")
        if not order_id:
            continue
        meta = {k: v for k, v in entry.items() if k != "order_id"}
        final_records.append({
            "order_id": order_id,
            "s3_url": url_map.get(order_id, ""),
            **meta,
        })

    save_json(config.FINAL_OUTPUT_FILE, final_records)
    log.info("Final output: %d records → %s", len(final_records), config.FINAL_OUTPUT_FILE)

    with_s3 = sum(1 for r in final_records if r["s3_url"])
    log.info("  With S3 URL: %d | Without: %d", with_s3, len(final_records) - with_s3)
    return final_records


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Orders PO Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        choices=["scrape", "download", "merge", "all"],
        default="all",
        help="Which phase to run (default: all)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser in visible mode (useful for debugging navigation)",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping; use existing po_catalog.json",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N orders (for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download + validate PDFs only — skip S3 upload",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-process only IDs listed in failed_ids.json",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=f"Override max concurrent workers (default: {config.MAX_WORKERS})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        metavar="N",
        help=f"Override batch size (default: {config.BATCH_SIZE})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        metavar="SEC",
        help=f"Override inter-request delay in seconds (default: {config.REQUEST_DELAY})",
    )
    parser.add_argument(
        "--order-ids",
        type=str,
        default=None,
        metavar="IDS",
        help="Comma-separated order IDs for download phase (skips po_catalog.json)",
    )
    args = parser.parse_args()

    # Apply CLI overrides
    if args.workers is not None:
        config.MAX_WORKERS = args.workers
    if args.batch_size is not None:
        config.BATCH_SIZE = args.batch_size
    if args.delay is not None:
        config.REQUEST_DELAY = args.delay

    order_ids_override: list[str] | None = None
    if args.order_ids:
        if args.retry_failed:
            log.error("Use either --order-ids or --retry-failed, not both.")
            sys.exit(1)
        order_ids_override = _parse_order_ids(args.order_ids)
        if not order_ids_override:
            log.error("--order-ids produced no IDs.")
            sys.exit(1)

    summary = RunSummary()

    log.info("=" * 60)
    log.info("Orders PO Extraction Pipeline")
    log.info(
        "Phase: %s | Limit: %s | Dry-run: %s | Retry-failed: %s",
        args.phase, args.limit or "none", args.dry_run, args.retry_failed,
    )
    log.info(
        "Workers: %d | Batch: %d | Delay: %.2fs",
        config.MAX_WORKERS, config.BATCH_SIZE, config.REQUEST_DELAY,
    )
    log.info("=" * 60)

    if args.phase in ("download", "all") and not args.dry_run:
        if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
            log.error(
                "AWS credentials not set. Check .env file. "
                "(Use --dry-run to skip S3.)"
            )
            sys.exit(1)

    s3_urls = None

    # Phase 1: Scrape
    if args.phase in ("scrape", "all") and not args.skip_scrape and not args.retry_failed:
        log.info("-" * 40)
        log.info("PHASE 1: Scraping all PO orders from UI")
        log.info("-" * 40)
        phase_scrape(headless=not args.headful)

    # Phase 2: Download + Upload
    if args.phase in ("download", "all"):
        log.info("-" * 40)
        if args.dry_run:
            log.info("PHASE 2: Download + Validate PDFs (DRY RUN — no S3)")
        else:
            log.info("PHASE 2: Download PDFs + Upload to S3")
        log.info("-" * 40)
        s3_urls = phase_download_and_upload(
            limit=args.limit,
            dry_run=args.dry_run,
            retry_failed=args.retry_failed,
            order_ids_override=order_ids_override,
            summary=summary,
        )

    # Phase 3: Merge
    if args.phase in ("merge", "all") and not args.dry_run:
        log.info("-" * 40)
        log.info("PHASE 3: Merging catalog + S3 URLs")
        log.info("-" * 40)
        phase_merge(s3_urls)

    summary.print_report(log)


if __name__ == "__main__":
    main()
