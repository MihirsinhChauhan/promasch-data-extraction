"""
Validate a sample of parsed records from parts_records.jsonl.

Prints a human-readable table for each record showing:
  display_name, entity_id_source, prices, qty, spec count, image count,
  S3 URL reachability (optional --check-s3).

Optionally diffs against the v1 merged JSON (--compare-v1 PATH) to check
for regressions: records missing in v2, large price deltas, spec coverage
improvement.

Usage:
  python validate_sample.py
  python validate_sample.py --limit 50
  python validate_sample.py --check-s3
  python validate_sample.py --compare-v1 ../construnctionary/data/final/constructionary_merged.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import requests

import config
from utils import load_json, load_jsonl, setup_logging

log = setup_logging("validate_sample")

# Width constants for the summary table
_COL = {
    "display_name": 50,
    "entity_id": 14,
    "price_market": 14,
    "price_lp": 14,
    "qty": 8,
    "specs": 7,
    "imgs": 6,
    "s3": 6,
    "warn": 40,
}


def _fmt(v: object, width: int) -> str:
    s = "-" if v is None or v == "" else str(v)
    return s[:width].ljust(width)


def _check_s3_url(url: str, timeout: float = 5.0) -> str:
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return "OK" if r.status_code < 400 else f"HTTP {r.status_code}"
    except Exception as e:
        return f"ERR:{str(e)[:20]}"


def _print_header() -> None:
    cols = [
        ("display_name", "Display Name"),
        ("entity_id", "entity_id"),
        ("price_market", "price_mkt"),
        ("price_lp", "price_lp"),
        ("qty", "qty"),
        ("specs", "specs"),
        ("imgs", "imgs"),
        ("s3", "s3"),
        ("warn", "warnings"),
    ]
    header = "  ".join(_fmt(label, _COL[key]) for key, label in cols)
    sep = "  ".join("-" * _COL[key] for key, _ in cols)
    print(header)
    print(sep)


def _print_record(rec: dict, *, check_s3: bool = False) -> None:
    dn = rec.get("display_name", "?")
    eid = rec.get("entity_id", "-")
    eid_src = rec.get("entity_id_source", "?")
    eid_disp = f"{eid}({'R' if eid_src == 'response' else 'H'})"

    pm = rec.get("price_market")
    pl = rec.get("price_last_purchase")
    qty = rec.get("purchase_qty") or rec.get("stock_qty")
    specs = len(rec.get("specifications", {}))
    imgs = len(rec.get("image_urls", []))
    s3_count = len(rec.get("image_s3_urls", []))
    warns = "; ".join(rec.get("_parse_warnings", []))

    s3_status = f"{s3_count}/{imgs}"
    if check_s3:
        s3_urls = rec.get("image_s3_urls", [])
        statuses = [_check_s3_url(u) for u in s3_urls[:2]]
        s3_status = ",".join(statuses) if statuses else "none"

    row = "  ".join([
        _fmt(dn, _COL["display_name"]),
        _fmt(eid_disp, _COL["entity_id"]),
        _fmt(f"{pm:.2f}" if pm is not None else None, _COL["price_market"]),
        _fmt(f"{pl:.2f}" if pl is not None else None, _COL["price_lp"]),
        _fmt(qty, _COL["qty"]),
        _fmt(specs, _COL["specs"]),
        _fmt(imgs, _COL["imgs"]),
        _fmt(s3_status, _COL["s3"]),
        _fmt(warns, _COL["warn"]),
    ])
    print(row)


def _diff_vs_v1(records_v2: list[dict], v1_path: Path) -> None:
    """Print a summary diff between v2 records and v1 merged JSON."""
    print(f"\n{'='*70}")
    print(f"V1 vs V2 diff — comparing against {v1_path.name}")
    print("=" * 70)

    v1_data = load_json(v1_path)
    if not v1_data:
        print("  [WARN] Could not load v1 JSON.")
        return

    # v1 JSON is either {"parts": [...]} or a plain list
    if isinstance(v1_data, dict):
        v1_parts = v1_data.get("parts", [])
    elif isinstance(v1_data, list):
        v1_parts = v1_data
    else:
        print("  [WARN] Unexpected v1 JSON structure.")
        return

    v1_by_name: dict[str, dict] = {}
    for p in v1_parts:
        dn = p.get("display_name") or p.get("id", "")
        if dn:
            v1_by_name[dn] = p

    v2_by_name: dict[str, dict] = {
        r["display_name"]: r for r in records_v2 if r.get("display_name")
    }

    only_in_v1 = set(v1_by_name) - set(v2_by_name)
    only_in_v2 = set(v2_by_name) - set(v1_by_name)
    common = set(v1_by_name) & set(v2_by_name)

    print(f"  v1 records: {len(v1_by_name):,}")
    print(f"  v2 records: {len(v2_by_name):,}")
    print(f"  Only in v1: {len(only_in_v1):,}")
    print(f"  Only in v2: {len(only_in_v2):,}")
    print(f"  Common:     {len(common):,}")

    # Price delta summary for common records
    price_deltas: list[float] = []
    for dn in list(common)[:1000]:
        v1p = v1_by_name[dn]
        v2p = v2_by_name[dn]
        v1_pm = v1p.get("price_market") or 0.0
        v2_pm = v2p.get("price_market") or 0.0
        if v1_pm and v2_pm:
            price_deltas.append(abs(v1_pm - v2_pm) / max(v1_pm, v2_pm))

    if price_deltas:
        avg_delta = sum(price_deltas) / len(price_deltas) * 100
        big_delta = sum(1 for d in price_deltas if d > 0.05)
        print(f"\n  Price deltas (sample of {len(price_deltas)}):")
        print(f"    Avg relative delta: {avg_delta:.1f}%")
        print(f"    Records with >5% delta: {big_delta}")

    # Spec coverage
    v2_with_specs = sum(1 for r in records_v2 if r.get("specifications"))
    v1_with_specs = sum(1 for p in v1_parts if p.get("specifications"))
    print(f"\n  Spec coverage: v1={v1_with_specs}, v2={v2_with_specs}")

    if only_in_v1:
        print(f"\n  Sample of records only in v1 (missing from v2):")
        for dn in sorted(only_in_v1)[:10]:
            print(f"    - {dn[:70]}")


def run_validate(
    *,
    data_dir: Path,
    limit: int = 100,
    check_s3: bool = False,
    compare_v1: Optional[Path] = None,
    warnings_only: bool = False,
) -> None:
    records_path = data_dir / "parts_records.jsonl"
    records = load_jsonl(records_path)

    if not records:
        print(f"[validate] No records found in {records_path}")
        return

    sample = records[:limit]

    # Summary stats
    total = len(records)
    with_prices = sum(1 for r in records if r.get("price_market") or r.get("price_last_purchase"))
    with_specs = sum(1 for r in records if r.get("specifications"))
    with_imgs = sum(1 for r in records if r.get("image_urls"))
    with_s3 = sum(1 for r in records if r.get("image_s3_urls"))
    hashed_id = sum(1 for r in records if r.get("entity_id_source") == "hashed")
    with_warnings = sum(1 for r in records if r.get("_parse_warnings"))

    print(f"\n{'='*70}")
    print(f"  CONSTRUCTIONARY V2 — VALIDATION REPORT")
    print(f"  Total records: {total:,}  |  Sample shown: {len(sample)}")
    print(f"{'='*70}")
    print(f"  With prices:     {with_prices:,} ({with_prices/total*100:.1f}%)")
    print(f"  With specs:      {with_specs:,} ({with_specs/total*100:.1f}%)")
    print(f"  With images:     {with_imgs:,} ({with_imgs/total*100:.1f}%)")
    print(f"  With S3 images:  {with_s3:,} ({with_s3/total*100:.1f}%)")
    print(f"  Hashed entity_id:{hashed_id:,} ({hashed_id/total*100:.1f}%)")
    print(f"  With warnings:   {with_warnings:,} ({with_warnings/total*100:.1f}%)")
    print(f"{'='*70}\n")

    if warnings_only:
        sample = [r for r in sample if r.get("_parse_warnings")]
        print(f"  (--warnings-only: showing {len(sample)} records with parse warnings)\n")

    _print_header()
    for rec in sample:
        _print_record(rec, check_s3=check_s3)

    if compare_v1:
        _diff_vs_v1(records, compare_v1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--limit", type=int, default=100, metavar="N",
                   help="Number of records to display (default 100)")
    p.add_argument("--check-s3", action="store_true",
                   help="HEAD-check each S3 URL for reachability")
    p.add_argument("--compare-v1", type=Path, default=None, metavar="PATH",
                   help="Path to v1 constructionary_merged.json for cross-pipeline diff")
    p.add_argument("--warnings-only", action="store_true",
                   help="Only show records that have parse warnings")
    args = p.parse_args()

    run_validate(
        data_dir=args.data_dir.expanduser().resolve(),
        limit=args.limit,
        check_s3=args.check_s3,
        compare_v1=args.compare_v1,
        warnings_only=args.warnings_only,
    )


if __name__ == "__main__":
    main()
