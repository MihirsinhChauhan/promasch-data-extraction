"""
Gap-fill collector: analyse existing batches to find under-extracted
categories, then re-collect only those via a selective tree walk —
clicking through every node but only aggressively scrolling gap categories.

Usage:
  # 1. Analyse gaps (dry-run, prints report + writes gap manifest):
  python gap_fill.py --analyse --batches-dir data

  # 2. Collect missing data for gap categories:
  python gap_fill.py --collect --batches-dir data --output-dir data/gap_fill --headful

  # 3. Merge gap-fill results into existing final/ directory:
  python gap_fill.py --merge --batches-dir data --gap-dir data/gap_fill --final-dir data/final
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set

from playwright.sync_api import sync_playwright

from collector import (
    _build_parts_scroll_js,
    clean,
    first_match,
    get_context,
    is_leaf_by_name,
    login_and_open_constructionary,
    parse_entity_id_from_payload,
    parse_expected_parts,
    _TREE_SELECTORS,
)
from gwt_parser import parse_dump_file, aggregate_parts


# ── Gap analysis ─────────────────────────────────────────────────────────────

_COVERAGE_THRESHOLD = 0.70  # flag categories with <70% estimated coverage


def _load_all_catalogs(batches_dir: Path) -> List[Dict[str, Any]]:
    """Load and merge payload_catalog.json from every batch_* subdirectory."""
    all_entries: List[Dict[str, Any]] = []
    for batch in sorted(batches_dir.glob("batch_*")):
        cat = batch / "payload_catalog.json"
        if cat.is_file():
            entries = json.loads(cat.read_text(encoding="utf-8"))
            for e in entries:
                e["_batch"] = batch.name
            all_entries.extend(entries)
    return all_entries


def _load_parsed_parts_count(batches_dir: Path) -> Dict[str, int]:
    """For each category, count how many parts were actually parsed across batches."""
    cat_parts: Dict[str, int] = defaultdict(int)
    for batch in sorted(batches_dir.glob("batch_*")):
        parsed_dir = batch / "parsed"
        catalog_path = batch / "payload_catalog.json"
        if not parsed_dir.exists() or not catalog_path.is_file():
            continue
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        stem_to_cat = {}
        for entry in catalog:
            dump_rel = entry.get("dump", "")
            stem = Path(dump_rel).stem
            stem_to_cat[stem] = entry.get("category", "")
        for pf in parsed_dir.glob("*.json"):
            cat_name = stem_to_cat.get(pf.stem, "")
            if not cat_name:
                continue
            try:
                doc = json.loads(pf.read_text(encoding="utf-8"))
                cat_parts[cat_name] += len(doc.get("parts", []))
            except Exception:
                pass
    return dict(cat_parts)


def analyse_gaps(batches_dir: Path) -> List[Dict[str, Any]]:
    """Return a list of under-extracted categories sorted by gap size."""
    entries = _load_all_catalogs(batches_dir)
    parsed_counts = _load_parsed_parts_count(batches_dir)

    cat_info: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        cat = e.get("category", "")
        if not cat:
            continue
        if cat not in cat_info:
            expected = parse_expected_parts(cat)
            cat_info[cat] = {
                "category": cat,
                "expected": expected,
                "entity_ids": set(),
                "rpcs": 0,
                "batches": set(),
            }
        cat_info[cat]["entity_ids"].add(e.get("entity_id", ""))
        cat_info[cat]["rpcs"] += 1
        cat_info[cat]["batches"].add(e.get("_batch", ""))

    gaps: List[Dict[str, Any]] = []
    for cat, info in cat_info.items():
        parsed = parsed_counts.get(cat, 0)
        expected = info["expected"]
        if expected <= 0:
            continue
        coverage = parsed / expected if expected else 1.0
        if coverage < _COVERAGE_THRESHOLD:
            gaps.append({
                "category": cat,
                "expected": expected,
                "parsed": parsed,
                "rpcs": info["rpcs"],
                "coverage": round(coverage, 3),
                "gap": expected - parsed,
                "entity_ids": sorted(info["entity_ids"]),
                "batches": sorted(info["batches"]),
            })

    # Also find categories that appear in no batch at all (batch_901_1000 failure)
    all_captured_cats = set(cat_info.keys())

    gaps.sort(key=lambda g: -g["gap"])
    return gaps


def print_analysis(gaps: List[Dict[str, Any]]) -> None:
    total_expected = sum(g["expected"] for g in gaps)
    total_parsed = sum(g["parsed"] for g in gaps)
    total_gap = sum(g["gap"] for g in gaps)
    print("=" * 72)
    print("GAP ANALYSIS REPORT")
    print("=" * 72)
    print(f"Under-extracted categories:  {len(gaps)}")
    print(f"Total expected parts:        {total_expected}")
    print(f"Total parsed parts:          {total_parsed}")
    print(f"Estimated gap:               {total_gap}")
    print(f"Coverage threshold:          {_COVERAGE_THRESHOLD:.0%}")
    print()
    print(f"{'Category':<55} {'Exp':>6} {'Got':>6} {'Gap':>6} {'Cov':>6}")
    print("-" * 85)
    for g in gaps[:30]:
        name = g["category"][:54]
        print(f"{name:<55} {g['expected']:>6} {g['parsed']:>6} {g['gap']:>6} {g['coverage']:>5.0%}")
    if len(gaps) > 30:
        print(f"  ... and {len(gaps) - 30} more")
    print()


# ── Targeted gap-fill collection via tree walk ───────────────────────────────

_IDLE_ROUNDS = 10
_SCROLL_WAIT_MS = 1500
_MAX_SCROLL = 800


def _normalize_category(name: str) -> str:
    """Normalise a category name for matching (lowercase, collapse whitespace)."""
    return re.sub(r"\s+", " ", name.lower().strip())


def _scroll_and_capture(
    ctx,
    seq: Dict[str, Any],
    expected_parts: int,
) -> int:
    """Scroll the parts panel aggressively. Returns new RPCs captured."""
    scroll_js = _build_parts_scroll_js()
    scrolled_via_js = False
    matched_sel = None
    try:
        matched_sel = ctx.evaluate(scroll_js)
        scrolled_via_js = True
        print(f"  [scroll] panel: {matched_sel or 'none found — fallback'}")
    except Exception:
        pass

    if not scrolled_via_js or not matched_sel:
        ctx.mouse.move(900, 450)
        print("  [scroll] panel: mouse fallback (900, 450)")

    rounds = max(_MAX_SCROLL, expected_parts // 10 + 20)
    rounds = min(rounds, _MAX_SCROLL)

    prev_count = seq["n"]
    idle = 0
    last_at = seq["n"]

    for _ in range(rounds):
        ctx.wait_for_timeout(_SCROLL_WAIT_MS)
        if scrolled_via_js and matched_sel:
            try:
                ctx.evaluate(scroll_js)
            except Exception:
                ctx.mouse.move(900, 450)
                ctx.mouse.wheel(0, 1800)
        else:
            ctx.mouse.wheel(0, 1800)

        if seq["n"] == last_at:
            idle += 1
            if idle >= _IDLE_ROUNDS:
                break
        else:
            idle = 0
            last_at = seq["n"]

    return seq["n"] - prev_count


def _dismiss_glass(ctx) -> None:
    """Wait for / remove the GWT loading glass overlay before interacting."""
    glass_sel = ".gwt-PopupPanelGlass"
    try:
        if ctx.locator(glass_sel).count() == 0:
            return
        print("  [wait] GWT glass overlay detected, waiting up to 60s...")
        ctx.wait_for_selector(glass_sel, state="hidden", timeout=60_000)
    except Exception:
        try:
            ctx.evaluate(
                "document.querySelectorAll('.gwt-PopupPanelGlass')"
                ".forEach(e => e.remove())"
            )
            ctx.wait_for_timeout(1000)
            print("  [wait] Glass overlay removed via JS")
        except Exception:
            pass


def _walk_gap_fill(
    ctx,
    tree_sel: str,
    max_folders: int,
    seq: Dict[str, Any],
    gap_set: Set[str],
) -> None:
    """Walk the tree like the regular collector, but only scroll/capture for
    categories that are in gap_set. Non-gap leaves are skipped entirely
    (no click) to avoid triggering slow loads and glass overlays."""
    visited: set = set()
    i = 0
    gap_found = 0
    gap_done = 0

    while i < max_folders:
        live_count = ctx.locator(tree_sel).count()
        if i >= live_count:
            break

        folder = ctx.locator(tree_sel).nth(i)
        try:
            name = clean(folder.inner_text(timeout=3000))
        except Exception:
            name = f"folder_{i + 1}"
        if not name:
            name = f"folder_{i + 1}"

        i += 1

        if name in visited:
            continue
        visited.add(name)

        is_leaf = is_leaf_by_name(name)

        # Skip non-gap leaves entirely — clicking them triggers heavy RPCs
        # and can cause glass overlays that block subsequent interaction.
        if is_leaf:
            name_norm = _normalize_category(name)
            if name_norm not in gap_set:
                continue

        if not is_leaf:
            seq["current_folder"] = name
            seq["current_category"] = ""
            print(f"\n[node {i}/{live_count}+] [FOLDER] {name}")
        else:
            seq["current_category"] = name

        _dismiss_glass(ctx)

        try:
            folder.click(timeout=10_000)
        except Exception as e:
            print(f"  [skip] click failed: {e}")
            _dismiss_glass(ctx)
            continue

        if not is_leaf:
            try:
                ctx.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            ctx.wait_for_timeout(1200)
            continue

        # This is a gap leaf — wait longer for large categories to load
        expected = parse_expected_parts(name)
        load_timeout = max(30_000, min(120_000, expected * 50))
        try:
            ctx.wait_for_load_state("networkidle", timeout=load_timeout)
        except Exception:
            pass
        ctx.wait_for_timeout(2000)
        _dismiss_glass(ctx)

        gap_found += 1
        print(f"\n[node {i}/{live_count}+] [GAP #{gap_found}] {name}")
        print(f"  expected={expected} parts")

        new_rpcs = _scroll_and_capture(ctx, seq, expected)
        est_parts = new_rpcs * 10
        print(f"  [done] {new_rpcs} RPCs → ~{est_parts} parts")

        if new_rpcs == 0:
            print(f"  [WARNING] No RPCs captured")

        gap_done += 1

    print(f"\n[gap-fill] Walk complete. Gap categories found: {gap_found}, scrolled: {gap_done}")


def run_gap_fill_collection(
    gaps: List[Dict[str, Any]],
    output_dir: Path,
    base_url: str,
    user: str,
    password: str,
    headful: bool,
) -> List[Dict[str, Any]]:
    """Walk the Constructionary tree, aggressively scroll only gap categories.

    Supports resuming: if output_dir already contains a payload_catalog.json
    from a previous run, categories already captured are removed from the gap
    set and new results are appended.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads_dir = output_dir / "payloads"
    dumps_dir = output_dir / "dumps"
    logs_dir = output_dir / "logs"
    for d in (payloads_dir, dumps_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    gap_set: Set[str] = {_normalize_category(g["category"]) for g in gaps}

    # ── Resume: load previous catalog and subtract already-done categories ───
    catalog: List[Dict[str, Any]] = []
    catalog_path = output_dir / "payload_catalog.json"
    if catalog_path.is_file():
        existing = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog.extend(existing)
        done_cats = {_normalize_category(e["category"]) for e in existing if e.get("category")}
        before = len(gap_set)
        gap_set -= done_cats
        print(f"[gap-fill] Resuming: {before - len(gap_set)} categories already done, {len(gap_set)} remaining")

    if not gap_set:
        print("[gap-fill] All gap categories already collected. Nothing to do.")
        return catalog

    print(f"[gap-fill] {len(gap_set)} gap categories to target")

    seq: Dict[str, Any] = {"n": len(catalog), "current_folder": "", "current_category": ""}

    def on_response(response) -> None:
        try:
            req = response.request
            if req.method != "POST":
                return
            if "deptherp/erp" not in req.url and "ERPService" not in req.url:
                return
            pd = req.post_data
            if not pd or "getPartDetails" not in pd:
                return
            if response.status != 200:
                return
            body = response.text()
        except Exception as e:
            with (logs_dir / "collector_errors.log").open("a", encoding="utf-8") as lf:
                lf.write(f"{time.time()}: response handler: {e}\n")
            return

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        payload_path = payloads_dir / f"{stem}.txt"
        dump_path = dumps_dir / f"{stem}.txt"
        payload_path.write_text(pd, encoding="utf-8")
        dump_path.write_text(body, encoding="utf-8")
        catalog.append({
            "dump": str(dump_path.relative_to(output_dir)),
            "payload": str(payload_path.relative_to(output_dir)),
            "captured_at_ms": int(time.time() * 1000),
            "url": response.url,
            "entity_id": parse_entity_id_from_payload(pd),
            "folder": seq.get("current_folder", ""),
            "category": seq.get("current_category", ""),
        })
        print(f"  [captured] getPartDetails → {stem}.txt")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()
        page.on("response", on_response)

        login_and_open_constructionary(page, base_url, user, password)
        ctx = get_context(page)

        tree_sel = None
        _, tree_sel = first_match(ctx, _TREE_SELECTORS)
        if not tree_sel:
            print("[gap-fill] No tree selector matched")
            (logs_dir / "no_tree_selector.txt").write_text(
                "No folder tree selector matched.\n", encoding="utf-8",
            )
        else:
            print(f"[gap-fill] Using tree selector: {tree_sel}")
            _walk_gap_fill(ctx, tree_sel, 5000, seq, gap_set)

        auth_path = output_dir / "auth_state.json"
        context.storage_state(path=str(auth_path))
        browser.close()

    catalog_path = output_dir / "payload_catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[gap-fill] Total RPCs captured: {len(catalog)} → {catalog_path}")
    return catalog


# ── Merge + deduplicate ──────────────────────────────────────────────────────

def merge_and_dedup(
    batches_dir: Path,
    gap_dir: Path,
    final_dir: Path,
) -> Path:
    """Parse gap-fill dumps, combine with existing final data, deduplicate by part id."""
    final_dir.mkdir(parents=True, exist_ok=True)

    # 1. Parse gap-fill dumps
    gap_parsed_dir = gap_dir / "parsed"
    gap_parsed_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = gap_dir / "payload_catalog.json"
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        cat_lookup: Dict[str, list] = {}
        for entry in catalog:
            stem = Path(entry["dump"]).stem
            folder = entry.get("folder", "")
            category = entry.get("category", "")
            cat_lookup[stem] = [x for x in [folder, category] if x]

        dumps_dir = gap_dir / "dumps"
        for df in sorted(dumps_dir.glob("*.txt")):
            out = gap_parsed_dir / f"{df.stem}.json"
            try:
                doc = parse_dump_file(df, category_path=cat_lookup.get(df.stem, []))
                out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"[merge] parse error {df.name}: {e}")

    # 2. Collect all parts: existing final + gap-fill parsed
    all_parts: Dict[str, Dict[str, Any]] = {}
    seen_ids: Set[str] = set()

    # Load existing final files
    for fj in sorted(final_dir.glob("constructionary_*.json")):
        try:
            data = json.loads(fj.read_text(encoding="utf-8"))
            for part in data.get("parts", []):
                pid = str(part.get("id", ""))
                if pid and pid not in seen_ids:
                    all_parts[pid] = part
                    seen_ids.add(pid)
        except Exception:
            pass

    # Load all batch output.json files
    for batch in sorted(batches_dir.glob("batch_*")):
        output_path = batch / "output.json"
        if output_path.is_file():
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
                for part in data.get("parts", []):
                    pid = str(part.get("id", ""))
                    if pid and pid not in seen_ids:
                        all_parts[pid] = part
                        seen_ids.add(pid)
            except Exception:
                pass

    # Load gap-fill parsed files
    gap_parts_added = 0
    for pf in sorted(gap_parsed_dir.glob("*.json")):
        try:
            doc = json.loads(pf.read_text(encoding="utf-8"))
            for part in doc.get("parts", []):
                pid = str(part.get("id", ""))
                if pid and pid not in seen_ids:
                    all_parts[pid] = part
                    seen_ids.add(pid)
                    gap_parts_added += 1
        except Exception:
            pass

    # 3. Write merged output
    merged = {
        "total": len(all_parts),
        "parts": list(all_parts.values()),
    }
    out_path = final_dir / "constructionary_merged.json"
    out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n[merge] Existing unique parts:  {len(seen_ids) - gap_parts_added}")
    print(f"[merge] New parts from gap-fill: {gap_parts_added}")
    print(f"[merge] Total merged (deduped):  {len(all_parts)}")
    print(f"[merge] Written → {out_path}")
    return out_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gap-fill: find under-extracted categories and re-collect them",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--analyse", action="store_true", help="Run gap analysis and write manifest")
    p.add_argument("--collect", action="store_true", help="Collect data for gap categories")
    p.add_argument("--merge", action="store_true", help="Parse gap dumps + merge + deduplicate")
    p.add_argument("--all", action="store_true", help="analyse → collect → merge")
    p.add_argument(
        "--batches-dir", type=Path, default=Path(__file__).resolve().parent / "data",
        help="Directory containing batch_* subdirectories",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help="Output directory for gap-fill collection (default: <batches-dir>/gap_fill)",
    )
    p.add_argument(
        "--gap-dir", type=Path, default=None,
        help="Gap-fill directory for merge (default: <batches-dir>/gap_fill)",
    )
    p.add_argument(
        "--final-dir", type=Path, default=None,
        help="Final output directory for merge (default: <batches-dir>/final)",
    )
    p.add_argument(
        "--manifest", type=Path, default=None,
        help="Path to gap manifest JSON (skip analysis, use this file)",
    )
    p.add_argument("--base-url", default="https://gw.promasch.in")
    p.add_argument("--user", default=os.getenv("CONSTRUCTIONARY_USER", "Vikram@greenwave.ws"))
    p.add_argument("--password", default=os.getenv("CONSTRUCTIONARY_PASSWORD", "Infosys@9009"))
    p.add_argument("--headful", action="store_true")
    p.add_argument(
        "--min-gap", type=int, default=5,
        help="Only target categories with at least this many missing parts (default: 5)",
    )
    p.add_argument(
        "--max-categories", type=int, default=0,
        help="Limit how many gap categories to collect (0=all)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    do_all = args.all
    do_analyse = args.analyse or do_all
    do_collect = args.collect or do_all
    do_merge = args.merge or do_all

    if not (do_analyse or do_collect or do_merge):
        raise SystemExit("Specify --analyse, --collect, --merge, or --all")

    batches_dir = args.batches_dir.resolve()
    output_dir = (args.output_dir or batches_dir / "gap_fill").resolve()
    gap_dir = (args.gap_dir or output_dir).resolve()
    final_dir = (args.final_dir or batches_dir / "final").resolve()
    manifest_path = args.manifest or (output_dir / "gap_manifest.json")

    gaps: List[Dict[str, Any]] = []

    # ── Analyse ──────────────────────────────────────────────────────────
    if do_analyse:
        print("[gap-fill] Analysing existing batches...")
        gaps = analyse_gaps(batches_dir)
        gaps = [g for g in gaps if g["gap"] >= args.min_gap]
        print_analysis(gaps)

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(gaps, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"[gap-fill] Manifest written → {manifest_path}")

    # ── Collect ──────────────────────────────────────────────────────────
    if do_collect:
        if not gaps and manifest_path.is_file():
            gaps = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"[gap-fill] Loaded {len(gaps)} gaps from {manifest_path}")
        if not gaps:
            raise SystemExit("No gaps found. Run --analyse first or provide --manifest.")

        if args.max_categories > 0:
            gaps = gaps[: args.max_categories]

        run_gap_fill_collection(
            gaps=gaps,
            output_dir=output_dir,
            base_url=args.base_url,
            user=args.user,
            password=args.password,
            headful=args.headful,
        )

    # ── Merge ────────────────────────────────────────────────────────────
    if do_merge:
        merge_and_dedup(batches_dir, gap_dir, final_dir)


if __name__ == "__main__":
    main()
