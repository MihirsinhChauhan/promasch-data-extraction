#!/usr/bin/env python3
"""
Vendor Bills Extraction Pipeline
=================================

Phases:
  1. Scrape metadata from Promasch UI (Playwright)
  2. Download PDFs (requests, parallel)
  3. Upload PDFs to S3 (boto3, parallel)
  4. Merge metadata + S3 URLs into final output

Usage:
  python main.py                          # Run full pipeline
  python main.py --phase scrape           # Phase 1 only
  python main.py --phase download         # Phase 2+3 (download + upload)
  python main.py --phase merge            # Phase 4 only
  python main.py --type PO               # Process only PO bills
  python main.py --headful               # Show browser during scrape
  python main.py --skip-scrape           # Use existing metadata, skip to download
  python main.py --limit 50              # Process only first 50 bills
  python main.py --dry-run               # Download + validate only, skip S3
  python main.py --retry-failed          # Re-process only IDs from failed_ids.json
  python main.py --workers 5             # Override concurrency
  python main.py --batch-size 100        # Override batch size
  python main.py --delay 0.2             # Override inter-request delay (seconds)
  python main.py --bill-ids 21405,21406  # Test download without po_metadata.json (use with --type PO|WO)
  python main.py --id-range 1-21405     # Try all IDs in a range (use with --type PO|WO)
"""

import argparse
import sys

import config
from utils import (
    setup_logging,
    load_json,
    save_json,
    get_processed_ids,
    get_failed_bill_ids,
    clear_failed_ids,
    RunSummary,
)

log = setup_logging("main")


def _parse_id_range(s: str) -> list[int]:
    """Parse 'START-END' into a list of ints [START, START+1, ..., END]."""
    parts = s.strip().split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected START-END format, got: {s!r}")
    start, end = int(parts[0]), int(parts[1])
    if start > end:
        raise ValueError(f"START ({start}) must be <= END ({end})")
    return list(range(start, end + 1))


def _parse_bill_ids_csv(s: str) -> list[int]:
    """Parse comma-separated bill IDs; dedupe while preserving order."""
    seen: set[int] = set()
    out: list[int] = []
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        try:
            bid = int(part)
        except ValueError as e:
            raise ValueError(f"not an integer: {part!r}") from e
        if bid not in seen:
            seen.add(bid)
            out.append(bid)
    return out


def phase_scrape(bill_type: str, headless: bool) -> dict:
    from playwright_scraper import scrape_bills, scrape_all

    if bill_type == "ALL":
        return scrape_all(headless=headless)
    else:
        records = scrape_bills(bill_type, headless=headless)
        return {bill_type.lower(): records}


def _s3_url_for(order_type: str, bill_id: int) -> str:
    return (
        f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}.amazonaws.com"
        f"/{order_type.lower()}/{bill_id}.pdf"
    )


def phase_download_and_upload(
    bill_type: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    retry_failed: bool = False,
    bill_ids_override: list[int] | None = None,
    summary: RunSummary | None = None,
) -> dict[str, dict[int, str]]:
    """
    For each bill type, download PDFs and upload to S3.
    Skips bills already in S3 (resume support).
    Returns {order_type: {bill_id: s3_url}}.
    """
    from pdf_downloader import download_batch
    from s3_uploader import upload_batch, check_existing_keys

    types_to_process = ["PO", "WO"] if bill_type == "ALL" else [bill_type]
    all_s3_urls: dict[str, dict[int, str]] = {}

    for otype in types_to_process:
        all_bill_ids: list[int] = []

        # Decide which IDs to process
        if retry_failed:
            remaining_ids = get_failed_bill_ids(config.FAILED_IDS_FILE, otype)
            if not remaining_ids:
                log.info("%s: No failed IDs to retry", otype)
                continue
            log.info("%s: Retrying %d previously failed IDs", otype, len(remaining_ids))
        elif bill_ids_override is not None:
            all_bill_ids = list(bill_ids_override)
            log.info("%s: Using %d bill ID(s) from --bill-ids (no metadata file)", otype, len(all_bill_ids))
            if not dry_run:
                log.info("Checking S3 for existing %s PDFs...", otype)
                existing_in_s3 = check_existing_keys(otype, all_bill_ids)
                log.info(
                    "%s: %d already in S3, %d to process",
                    otype, len(existing_in_s3), len(all_bill_ids) - len(existing_in_s3),
                )
                remaining_ids = [bid for bid in all_bill_ids if bid not in existing_in_s3]
            else:
                existing_in_s3 = set()
                remaining_ids = list(all_bill_ids)
        else:
            metadata_file = config.PO_METADATA_FILE if otype == "PO" else config.WO_METADATA_FILE
            metadata = load_json(metadata_file)
            if not metadata:
                log.warning(
                    "No metadata for %s — run: python main.py --phase scrape --type %s "
                    "or test without metadata: python main.py --phase download --type %s "
                    "--bill-ids <id1,id2,...> --dry-run",
                    otype, otype, otype,
                )
                continue

            all_bill_ids = [r["bill_id"] for r in metadata if r.get("bill_id")]
            log.info("%s: %d total bill IDs from metadata", otype, len(all_bill_ids))

            if not dry_run:
                log.info("Checking S3 for existing %s PDFs...", otype)
                existing_in_s3 = check_existing_keys(otype, all_bill_ids)
                log.info(
                    "%s: %d already in S3, %d to process",
                    otype, len(existing_in_s3), len(all_bill_ids) - len(existing_in_s3),
                )
                remaining_ids = [bid for bid in all_bill_ids if bid not in existing_in_s3]
            else:
                existing_in_s3 = set()
                remaining_ids = list(all_bill_ids)

        # Apply --limit
        if limit is not None and len(remaining_ids) > limit:
            log.info("%s: Applying --limit %d (from %d)", otype, limit, len(remaining_ids))
            remaining_ids = remaining_ids[:limit]

        if not remaining_ids:
            log.info("%s: Nothing to process — skipping", otype)
            if not retry_failed and all_bill_ids:
                all_s3_urls[otype] = {bid: _s3_url_for(otype, bid) for bid in all_bill_ids}
            continue

        s3_urls: dict[int, str] = {}

        # Seed with already-existing S3 URLs
        if not retry_failed and not dry_run:
            for bid in existing_in_s3:
                s3_urls[bid] = _s3_url_for(otype, bid)

        batch_size = config.BATCH_SIZE
        total_downloaded = 0
        total_uploaded = 0
        total_failed = 0

        for batch_start in range(0, len(remaining_ids), batch_size):
            batch = remaining_ids[batch_start:batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (len(remaining_ids) + batch_size - 1) // batch_size
            log.info(
                "%s batch %d/%d: downloading %d PDFs",
                otype, batch_num, total_batches, len(batch),
            )

            pdf_map = download_batch(batch, otype)
            total_downloaded += len(pdf_map)
            total_failed += len(batch) - len(pdf_map)

            if pdf_map and not dry_run:
                batch_urls = upload_batch(pdf_map, otype)
                s3_urls.update(batch_urls)
                total_uploaded += len(batch_urls)
                log.info(
                    "%s batch %d: %d downloaded, %d uploaded",
                    otype, batch_num, len(pdf_map), len(batch_urls),
                )
            elif dry_run and pdf_map:
                log.info(
                    "%s batch %d: %d downloaded + validated (dry run, S3 skipped)",
                    otype, batch_num, len(pdf_map),
                )

        # Clean retried IDs that succeeded from failed_ids.json
        if retry_failed and total_downloaded > 0:
            succeeded_ids = list(pdf_map.keys()) if pdf_map else []
            if succeeded_ids:
                clear_failed_ids(config.FAILED_IDS_FILE, otype, succeeded_ids)
                log.info("%s: Cleared %d retried IDs from failed_ids.json", otype, len(succeeded_ids))

        all_s3_urls[otype] = s3_urls

        if summary:
            summary.add(
                f"{otype} Download",
                total=len(remaining_ids),
                success=total_downloaded,
                failed=total_failed,
            )
            if not dry_run:
                summary.add(
                    f"{otype} S3 Upload",
                    total=total_downloaded,
                    success=total_uploaded,
                    failed=total_downloaded - total_uploaded,
                )

    return all_s3_urls


def phase_merge(s3_urls: dict[str, dict[int, str]] | None = None) -> list:
    """
    Merge metadata with S3 URLs into final output.
    If s3_urls not provided, tries to reconstruct from existing final_output.
    """
    final_records = []

    for otype in ["PO", "WO"]:
        metadata_file = config.PO_METADATA_FILE if otype == "PO" else config.WO_METADATA_FILE
        metadata = load_json(metadata_file)
        if not metadata:
            continue

        type_s3_urls = (s3_urls or {}).get(otype, {})

        existing_final = load_json(config.FINAL_OUTPUT_FILE)
        existing_url_map = {
            r["bill_id"]: r["s3_url"]
            for r in existing_final
            if r.get("type") == otype and r.get("s3_url")
        }
        existing_url_map.update(type_s3_urls)

        for record in metadata:
            bill_id = record.get("bill_id")
            if not bill_id:
                continue

            meta_fields = {k: v for k, v in record.items() if k not in ("bill_id", "type")}

            final_records.append({
                "bill_id": bill_id,
                "type": otype,
                "s3_url": existing_url_map.get(bill_id, ""),
                "metadata": meta_fields,
            })

    save_json(config.FINAL_OUTPUT_FILE, final_records)
    log.info("Final output: %d records saved to %s", len(final_records), config.FINAL_OUTPUT_FILE)

    with_s3 = sum(1 for r in final_records if r["s3_url"])
    without_s3 = len(final_records) - with_s3
    log.info("  With S3 URL: %d | Without: %d", with_s3, without_s3)

    return final_records


def main():
    parser = argparse.ArgumentParser(
        description="Vendor Bills Extraction Pipeline",
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
        "--type",
        choices=["PO", "WO", "ALL"],
        default="ALL",
        help="Bill type to process (default: ALL)",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser in visible mode for debugging",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip scraping, use existing metadata files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only first N bills (for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and validate PDFs only — skip S3 upload",
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
        "--bill-ids",
        type=str,
        default=None,
        metavar="IDS",
        help="Comma-separated bill IDs for download phase (skips metadata JSON). Requires --type PO or WO.",
    )
    parser.add_argument(
        "--id-range",
        type=str,
        default=None,
        metavar="START-END",
        help="Try all bill IDs from START to END inclusive (e.g. 1-21405). Requires --type PO or WO.",
    )
    args = parser.parse_args()

    # Apply CLI overrides to config
    if args.workers is not None:
        config.MAX_WORKERS = args.workers
    if args.batch_size is not None:
        config.BATCH_SIZE = args.batch_size
    if args.delay is not None:
        config.REQUEST_DELAY = args.delay

    bill_ids_override: list[int] | None = None
    if args.bill_ids and args.id_range:
        log.error("Use either --bill-ids or --id-range, not both.")
        sys.exit(1)

    if args.bill_ids:
        if args.type == "ALL":
            log.error("--bill-ids requires --type PO or --type WO (not ALL).")
            sys.exit(1)
        if args.retry_failed:
            log.error("Use either --bill-ids or --retry-failed, not both.")
            sys.exit(1)
        try:
            bill_ids_override = _parse_bill_ids_csv(args.bill_ids)
        except ValueError as e:
            log.error("Invalid --bill-ids: %s", e)
            sys.exit(1)
        if not bill_ids_override:
            log.error("--bill-ids produced no IDs.")
            sys.exit(1)

    if args.id_range:
        if args.type == "ALL":
            log.error("--id-range requires --type PO or --type WO (not ALL).")
            sys.exit(1)
        if args.retry_failed:
            log.error("Use either --id-range or --retry-failed, not both.")
            sys.exit(1)
        try:
            bill_ids_override = _parse_id_range(args.id_range)
        except ValueError as e:
            log.error("Invalid --id-range: %s", e)
            sys.exit(1)
        log.info("ID range: %d IDs (%s)", len(bill_ids_override), args.id_range)

    summary = RunSummary()

    log.info("=" * 60)
    log.info("Vendor Bills Extraction Pipeline")
    log.info(
        "Phase: %s | Type: %s | Limit: %s | Dry-run: %s | Retry-failed: %s | Bill-ids: %s",
        args.phase, args.type,
        args.limit or "none",
        args.dry_run,
        args.retry_failed,
        len(bill_ids_override) if bill_ids_override else "none",
    )
    log.info(
        "Workers: %d | Batch size: %d | Request delay: %.2fs",
        config.MAX_WORKERS, config.BATCH_SIZE, config.REQUEST_DELAY,
    )
    log.info("=" * 60)

    # Validate AWS credentials (not needed for dry-run)
    if args.phase in ("download", "all") and not args.dry_run:
        if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
            log.error("AWS credentials not set. Check .env file. (Use --dry-run to skip S3.)")
            sys.exit(1)

    s3_urls = None

    # Phase 1: Scrape
    if args.phase in ("scrape", "all") and not args.skip_scrape and not args.retry_failed:
        log.info("-" * 40)
        log.info("PHASE 1: Scraping metadata from UI")
        log.info("-" * 40)
        phase_scrape(args.type, headless=not args.headful)

    # Phase 2+3: Download + Upload
    if args.phase in ("download", "all"):
        log.info("-" * 40)
        if args.dry_run:
            log.info("PHASE 2: Download + Validate PDFs (DRY RUN — no S3)")
        else:
            log.info("PHASE 2+3: Download PDFs + Upload to S3")
        log.info("-" * 40)
        s3_urls = phase_download_and_upload(
            args.type,
            limit=args.limit,
            dry_run=args.dry_run,
            retry_failed=args.retry_failed,
            bill_ids_override=bill_ids_override,
            summary=summary,
        )

    # Phase 4: Merge (skip on dry-run since no S3 URLs were produced)
    if args.phase in ("merge", "all") and not args.dry_run:
        log.info("-" * 40)
        log.info("PHASE 4: Merging metadata + S3 URLs")
        log.info("-" * 40)
        phase_merge(s3_urls)

    summary.print_report(log)


if __name__ == "__main__":
    main()
