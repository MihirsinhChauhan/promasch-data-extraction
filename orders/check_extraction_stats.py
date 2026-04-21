#!/usr/bin/env python3
"""
Summarize orders extraction: scraped catalog, merged output, and failed IDs.

Usage:
  python check_extraction_stats.py
  python check_extraction_stats.py --expected 11737   # show missing vs total
  python check_extraction_stats.py --json             # machine-readable stdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config
from utils import load_json


def _file_size(path: Path) -> int | None:
    if not path.exists():
        return None
    return path.stat().st_size


def _catalog_stats(catalog: list, recent_window: int = 300) -> dict:
    rows = len(catalog) if isinstance(catalog, list) else 0
    with_id = 0
    seen: set[int] = set()
    dupes = 0
    for e in catalog if isinstance(catalog, list) else []:
        oid = e.get("order_id") if isinstance(e, dict) else None
        if not oid:
            continue
        with_id += 1
        try:
            n = int(oid)
        except (ValueError, TypeError):
            continue
        if n in seen:
            dupes += 1
        else:
            seen.add(n)

    gap_stats: dict = {}
    if seen:
        lo, hi = min(seen), max(seen)
        full = set(range(lo, hi + 1))
        gaps = sorted(full - seen)
        recent_gaps = [g for g in gaps if g > hi - recent_window]
        old_gaps = [g for g in gaps if g <= hi - recent_window]
        gap_stats = {
            "order_id_min": lo,
            "order_id_max": hi,
            "sequence_range": hi - lo + 1,
            "total_gaps_in_sequence": len(gaps),
            "recent_gaps": recent_gaps,   # last `recent_window` IDs — likely in-process / new
            "old_gaps": len(old_gaps),    # historical — likely non-PO / cancelled IDs
        }

    return {
        "rows": rows,
        "rows_with_order_id": with_id,
        "unique_order_ids": len(seen),
        "duplicate_order_id_rows": dupes,
        **gap_stats,
    }


def _final_stats(records: list) -> dict:
    if not isinstance(records, list):
        return {"rows": 0, "with_s3_url": 0, "without_s3_url": 0}
    with_s3 = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        url = (r.get("s3_url") or "").strip()
        if url:
            with_s3 += 1
    n = len(records)
    return {
        "rows": n,
        "with_s3_url": with_s3,
        "without_s3_url": n - with_s3,
    }


def _failed_stats(data) -> dict:
    if isinstance(data, dict):
        po = data.get("po", [])
    elif isinstance(data, list):
        po = data
    else:
        po = []
    return {"failed_order_entries": len(po)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Orders extraction summary")
    parser.add_argument(
        "--expected",
        type=int,
        default=None,
        metavar="N",
        help="Known total POs in the system — used to compute missing count",
    )
    parser.add_argument(
        "--recent-window",
        type=int,
        default=300,
        metavar="N",
        help="Recent ID window for gap analysis (default: 300)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object to stdout",
    )
    args = parser.parse_args()

    data_dir = config.DATA_DIR
    paths = {
        "po_catalog": config.PO_CATALOG_FILE,
        "final_output": config.FINAL_OUTPUT_FILE,
        "failed_ids": config.FAILED_IDS_FILE,
    }

    catalog = load_json(paths["po_catalog"])
    catalog_stats = _catalog_stats(
        catalog if isinstance(catalog, list) else [],
        recent_window=args.recent_window,
    )

    final_raw = load_json(paths["final_output"])
    final_exists = paths["final_output"].exists() and final_raw
    final_stats = _final_stats(final_raw if isinstance(final_raw, list) else [])

    failed_raw = load_json(paths["failed_ids"])
    failed_stats = _failed_stats(failed_raw)

    report = {
        "data_dir": str(data_dir),
        "expected_total": args.expected,
        "files": {
            k: {
                "path": str(v),
                "bytes": _file_size(v),
                "exists": v.exists(),
            }
            for k, v in paths.items()
        },
        "po_catalog": catalog_stats,
        "final_output": final_stats,
        "failed_ids": failed_stats,
    }

    if args.expected:
        extracted = catalog_stats["unique_order_ids"]
        recent_gaps = len(catalog_stats.get("recent_gaps", []))
        missing = args.expected - extracted - recent_gaps
        report["gap_summary"] = {
            "expected": args.expected,
            "extracted": extracted,
            "in_process_recent_gaps": recent_gaps,
            "missing_not_accounted_for": missing,
        }

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("Orders extraction summary")
    print(f"  Data dir: {data_dir}")
    print()
    print("  Files:")
    for label, p in paths.items():
        sz = _file_size(p)
        sz_s = f"{sz:,} B" if sz is not None else "(missing)"
        print(f"    {label}: {p.name} — {sz_s}")
    print()
    print("  Scrape (po_catalog.json):")
    print(f"    Total rows:              {catalog_stats['rows']}")
    print(f"    Rows with order_id:      {catalog_stats['rows_with_order_id']}")
    print(f"    Unique order_id values:  {catalog_stats['unique_order_ids']}")
    if catalog_stats.get("duplicate_order_id_rows"):
        print(f"    Duplicate order_id rows: {catalog_stats['duplicate_order_id_rows']}")
    if "order_id_min" in catalog_stats:
        print()
        print("  Order ID range:")
        print(f"    Min ID:                   {catalog_stats['order_id_min']}")
        print(f"    Max ID:                   {catalog_stats['order_id_max']}")
        print(f"    Sequence range (min–max): {catalog_stats['sequence_range']}")
        print(f"    Total gaps in sequence:   {catalog_stats['total_gaps_in_sequence']}")
        recent_gaps = catalog_stats.get("recent_gaps", [])
        old_gaps = catalog_stats.get("old_gaps", 0)
        print(f"    Historical gaps (non-PO / cancelled): {old_gaps}")
        print(f"    Recent gaps (last {args.recent_window} ID range): {len(recent_gaps)}"
              + (" — likely in-process" if recent_gaps else ""))
        if recent_gaps:
            preview = recent_gaps[:10]
            suffix = f" … (+{len(recent_gaps)-10} more)" if len(recent_gaps) > 10 else ""
            print(f"      IDs: {preview}{suffix}")
    print()
    if args.expected:
        extracted = catalog_stats["unique_order_ids"]
        recent_gaps = len(catalog_stats.get("recent_gaps", []))
        missing = args.expected - extracted - recent_gaps
        print("  Gap summary:")
        print(f"    Expected total (system):   {args.expected}")
        print(f"    Extracted (catalog):       {extracted}")
        print(f"    In-process (recent gaps):  {recent_gaps}")
        print(f"    Missing / not scraped:     {missing}")
        print()
    if final_exists:
        print("  Merge (final_output.json):")
        print(f"    Total rows:     {final_stats['rows']}")
        print(f"    With S3 URL:    {final_stats['with_s3_url']}")
        print(f"    Without S3 URL: {final_stats['without_s3_url']}")
    else:
        print("  Merge (final_output.json): not present or empty — run merge phase after download/upload.")
    print()
    fe = failed_stats["failed_order_entries"]
    print(f"  Failed downloads (failed_ids.json): {fe} entr{'y' if fe == 1 else 'ies'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
