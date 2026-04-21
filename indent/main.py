#!/usr/bin/env python3
"""
Indent Extraction Pipeline
==========================

Phases:
  1. Scrape   — Playwright capture + replay + detail parse, then build
                data/indent_catalog.json.
  2. Download — Parallel PDF downloads ONLY for records where pdf_url exists.
                (Indent often has no direct PDF URL in payloads.)
  3. Merge    — Join catalog with S3 URLs into data/final_output.json.

Usage:
  python main.py                         # Full pipeline
  python main.py --phase scrape          # Phase 1 only
  python main.py --phase download        # Phase 2 only
  python main.py --phase merge           # Phase 3 only
  python main.py --headful               # Show browser during scrape
  python main.py --skip-scrape           # Use existing indent_catalog.json
  python main.py --dry-run               # Download + validate only, skip S3
  python main.py --retry-failed          # Re-process IDs from failed_ids.json
  python main.py --limit 50              # Process only first 50 indent IDs
  python main.py --indent-ids A1,B2      # Download only these indent IDs
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import config
from utils import (
    RunSummary,
    clear_failed_ids,
    get_failed_indent_ids,
    load_json,
    save_json,
    setup_logging,
)

log = setup_logging("main")


def _parse_indent_ids(s: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        if part not in seen:
            seen.add(part)
            out.append(part)
    return out


def _s3_url_for(indent_id: str) -> str:
    return (
        f"https://{config.AWS_BUCKET_NAME}.s3.{config.AWS_REGION}"
        f".amazonaws.com/indent/{indent_id}.pdf"
    )


def _load_catalog() -> list[dict[str, Any]]:
    catalog = load_json(config.INDENT_CATALOG_FILE)
    return catalog if isinstance(catalog, list) else []


def phase_scrape(headless: bool, *, workers: int, wait_seconds: int, page_size: int) -> list[dict[str, Any]]:
    from playwright_scraper import scrape_indent

    return scrape_indent(
        headless=headless,
        workers=workers,
        wait_seconds=wait_seconds,
        page_size=page_size,
    )


def phase_download_and_upload(
    *,
    limit: int | None = None,
    dry_run: bool = False,
    retry_failed: bool = False,
    indent_ids_override: list[str] | None = None,
    summary: RunSummary | None = None,
) -> dict[str, str]:
    from pdf_downloader import download_batch
    from s3_uploader import check_existing_keys, upload_batch

    catalog = _load_catalog()
    if not catalog and not retry_failed and indent_ids_override is None:
        log.warning(
            "indent_catalog.json is empty. Run:\n"
            "  python main.py --phase scrape"
        )
        return {}

    catalog_by_id = {
        str(rec.get("indent_id")): rec
        for rec in catalog
        if rec.get("indent_id")
    }

    if retry_failed:
        all_ids = get_failed_indent_ids(config.FAILED_IDS_FILE)
        if not all_ids:
            log.info("No failed IDs to retry")
            return {}
        log.info("Retrying %d previously failed indent ID(s)", len(all_ids))
    elif indent_ids_override is not None:
        all_ids = list(indent_ids_override)
        log.info("Using %d indent ID(s) from --indent-ids", len(all_ids))
    else:
        all_ids = list(catalog_by_id.keys())
        log.info("%d total indent IDs from indent_catalog.json", len(all_ids))

    if limit is not None and len(all_ids) > limit:
        log.info("Applying --limit %d (from %d)", limit, len(all_ids))
        all_ids = all_ids[:limit]

    if not all_ids:
        log.info("No indent IDs selected for processing")
        return {}

    s3_urls: dict[str, str] = {}
    remaining_ids = list(all_ids)
    if not dry_run:
        existing = check_existing_keys(all_ids)
        log.info("%d already in S3, %d to process", len(existing), len(all_ids) - len(existing))
        for indent_id in existing:
            s3_urls[indent_id] = _s3_url_for(indent_id)
        remaining_ids = [iid for iid in all_ids if iid not in existing]

    if not remaining_ids:
        log.info("Nothing to process — all selected IDs already uploaded")
        return s3_urls

    # For Indent, pdf_url may be empty. We do not fail these IDs; we skip them.
    url_map: dict[str, str] = {}
    missing_pdf_url = 0
    for indent_id in remaining_ids:
        row = catalog_by_id.get(indent_id, {})
        pdf_url = str(row.get("pdf_url") or "")
        if not pdf_url and config.INDENT_PDF_URL_TEMPLATE:
            pdf_url = config.INDENT_PDF_URL_TEMPLATE.format(indent_id=indent_id)
        if not pdf_url:
            missing_pdf_url += 1
            continue
        url_map[indent_id] = pdf_url

    if missing_pdf_url:
        log.warning(
            "%d indent ID(s) skipped because pdf_url is missing. "
            "Set INDENT_PDF_URL_TEMPLATE or enrich catalog if PDFs are required.",
            missing_pdf_url,
        )

    total_downloaded = 0
    total_uploaded = 0
    total_failed = 0

    if url_map:
        batch_size = config.BATCH_SIZE
        ids = list(url_map.keys())
        for batch_start in range(0, len(ids), batch_size):
            batch_ids = ids[batch_start: batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (len(ids) + batch_size - 1) // batch_size
            batch_map = {iid: url_map[iid] for iid in batch_ids}

            log.info("Batch %d/%d: downloading %d indent PDF(s)", batch_num, total_batches, len(batch_map))
            pdf_map = download_batch(batch_map)
            total_downloaded += len(pdf_map)
            total_failed += len(batch_map) - len(pdf_map)

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

    if retry_failed and s3_urls:
        clear_failed_ids(config.FAILED_IDS_FILE, list(s3_urls.keys()))

    if summary:
        summary.add("Indent Download", total=len(url_map), success=total_downloaded, failed=total_failed)
        if not dry_run:
            summary.add(
                "Indent S3 Upload",
                total=total_downloaded,
                success=total_uploaded,
                failed=total_downloaded - total_uploaded,
            )

    return s3_urls


def phase_merge(s3_urls: dict[str, str] | None = None) -> list[dict[str, Any]]:
    catalog = _load_catalog()
    if not catalog:
        log.warning("indent_catalog.json is empty — merge skipped")
        return []

    existing_final = load_json(config.FINAL_OUTPUT_FILE)
    url_map: dict[str, str] = {
        str(r["indent_id"]): r["s3_url"]
        for r in existing_final
        if r.get("indent_id") and r.get("s3_url")
    } if isinstance(existing_final, list) else {}
    url_map.update(s3_urls or {})

    final_records: list[dict[str, Any]] = []
    for entry in catalog:
        indent_id = str(entry.get("indent_id") or "")
        if not indent_id:
            continue
        meta = {k: v for k, v in entry.items() if k not in ("indent_id", "pdf_url")}
        final_records.append({
            "indent_id": indent_id,
            "pdf_url": entry.get("pdf_url", ""),
            "s3_url": url_map.get(indent_id, ""),
            **meta,
        })

    save_json(config.FINAL_OUTPUT_FILE, final_records)
    with_s3 = sum(1 for r in final_records if r.get("s3_url"))
    without_pdf_url = sum(1 for r in final_records if not r.get("pdf_url"))
    log.info("Final output: %d records → %s", len(final_records), config.FINAL_OUTPUT_FILE)
    log.info("  With S3 URL: %d | Without S3: %d", with_s3, len(final_records) - with_s3)
    log.info("  Without PDF URL: %d", without_pdf_url)
    return final_records


def phase_probe_pdf_urls(
    *,
    sample_size: int,
    indent_ids_override: list[str] | None = None,
    update_catalog: bool = False,
) -> str | None:
    from pdf_url_probe import backfill_catalog_pdf_urls, probe_templates

    catalog = _load_catalog()
    if indent_ids_override is not None:
        probe_ids = list(indent_ids_override)
    else:
        probe_ids = [
            str(row.get("indent_id"))
            for row in catalog
            if row.get("indent_id")
        ]
    if sample_size > 0:
        probe_ids = probe_ids[:sample_size]

    if not probe_ids:
        log.warning("No indent IDs available for PDF URL probe.")
        return None

    best, results = probe_templates(probe_ids)
    if not results:
        log.warning("PDF URL probe could not test any template.")
        return None

    top = results[0]
    log.info(
        "PDF URL probe: best success=%d/%d template=%s",
        top.success, top.tested, top.template,
    )
    if top.sample_success_urls:
        log.info("Probe sample success URL: %s", top.sample_success_urls[0])
    if not best:
        log.warning("No working PDF URL template found for sampled IDs.")
        return None

    if update_catalog:
        updated = backfill_catalog_pdf_urls(best)
        log.info("Backfilled pdf_url for %d catalog record(s)", updated)

    return best


def main():
    parser = argparse.ArgumentParser(
        description="Indent Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--phase", choices=["scrape", "download", "merge", "all"], default="all")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible mode")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scrape phase")
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    parser.add_argument("--dry-run", action="store_true", help="Download + validate only, skip S3")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--workers", type=int, default=None, metavar="N")
    parser.add_argument("--batch-size", type=int, default=None, metavar="N")
    parser.add_argument("--delay", type=float, default=None, metavar="SEC")
    parser.add_argument("--wait", type=int, default=60, metavar="SEC", help="UI wait window during scrape")
    parser.add_argument("--page-size", type=int, default=100, metavar="N", help="Indent list page size")
    parser.add_argument("--indent-ids", type=str, default=None, metavar="IDS")
    parser.add_argument(
        "--probe-pdf-urls",
        action="store_true",
        help="Probe common indent PDF endpoint patterns using sample IDs",
    )
    parser.add_argument(
        "--probe-update-catalog",
        action="store_true",
        help="When probe finds a template, fill missing pdf_url in indent_catalog.json",
    )
    parser.add_argument(
        "--probe-sample-size",
        type=int,
        default=15,
        metavar="N",
        help="Sample size for URL probing (default: 15)",
    )
    args = parser.parse_args()

    if args.workers is not None:
        config.MAX_WORKERS = args.workers
    if args.batch_size is not None:
        config.BATCH_SIZE = args.batch_size
    if args.delay is not None:
        config.REQUEST_DELAY = args.delay

    indent_ids_override: list[str] | None = None
    if args.indent_ids:
        if args.retry_failed:
            log.error("Use either --indent-ids or --retry-failed, not both.")
            sys.exit(1)
        indent_ids_override = _parse_indent_ids(args.indent_ids)
        if not indent_ids_override:
            log.error("--indent-ids produced no IDs.")
            sys.exit(1)

    if args.phase in ("download", "all") and not args.dry_run:
        if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
            log.error(
                "AWS credentials not set. Check .env file. "
                "(Use --dry-run to skip S3.)"
            )
            sys.exit(1)

    summary = RunSummary()
    s3_urls: dict[str, str] | None = None
    discovered_pdf_template: str | None = None

    log.info("=" * 60)
    log.info("Indent Extraction Pipeline")
    log.info(
        "Phase: %s | Limit: %s | Dry-run: %s | Retry-failed: %s",
        args.phase, args.limit or "none", args.dry_run, args.retry_failed,
    )
    log.info(
        "Workers: %d | Batch: %d | Delay: %.2fs",
        config.MAX_WORKERS, config.BATCH_SIZE, config.REQUEST_DELAY,
    )
    log.info("=" * 60)

    if args.phase in ("scrape", "all") and not args.skip_scrape and not args.retry_failed:
        log.info("-" * 40)
        log.info("PHASE 1: Scraping Indent Completed data")
        log.info("-" * 40)
        phase_scrape(
            headless=not args.headful,
            workers=config.MAX_WORKERS,
            wait_seconds=args.wait,
            page_size=args.page_size,
        )

    if args.probe_pdf_urls:
        log.info("-" * 40)
        log.info("PDF URL PROBE: testing indent PDF endpoints")
        log.info("-" * 40)
        discovered_pdf_template = phase_probe_pdf_urls(
            sample_size=args.probe_sample_size,
            indent_ids_override=indent_ids_override,
            update_catalog=args.probe_update_catalog,
        )
        if discovered_pdf_template:
            config.INDENT_PDF_URL_TEMPLATE = discovered_pdf_template
            log.info("Using discovered PDF template for this run.")

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
            indent_ids_override=indent_ids_override,
            summary=summary,
        )

    if args.phase in ("merge", "all") and not args.dry_run:
        log.info("-" * 40)
        log.info("PHASE 3: Merging catalog + S3 URLs")
        log.info("-" * 40)
        phase_merge(s3_urls)

    summary.print_report(log)


if __name__ == "__main__":
    main()
