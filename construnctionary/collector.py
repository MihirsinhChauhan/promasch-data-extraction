"""
Playwright collector: login, navigate Constructionary, intercept GWT getPartDetails
RPCs, save request payloads + response bodies and auth state for replay.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Page, sync_playwright

# ── Same tree selectors as UI-extraction (Constructionary DOM) ───────────────

_TREE_SELECTORS = [
    ".folderTileNew, .categoryTileNew",
    ".folderTileNew",
    ".categoryTileNew",
    ".folderName",
    ".gwt-TreeItem",
    "td.gwt-TreeItem",
    "[class*='TreeItem']",
    "[class*='treeItem']",
    "[class*='tree-node']",
    "[class*='folderItem']",
    "[class*='folder-row']",
    "[class*='folder-item']",
]


def clean(text: str) -> str:
    return " ".join(text.split()).strip()


def first_match(ctx, selectors: List[str]) -> Tuple[Optional[Any], Optional[str]]:
    for sel in selectors:
        try:
            loc = ctx.locator(sel)
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, None


_USER_INPUT_SEL = (
    'input[type="text"], input[type="email"], input[name*="user"], '
    'input[name*="email"], input[id*="user"], input[id*="email"], '
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="checkbox"])'
)


def login_and_open_constructionary(page: Page, base_url: str, user: str, password: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    # Wait up to 60s for the login form to appear before trying to fill it.
    page.wait_for_selector(_USER_INPUT_SEL, timeout=60_000)
    page.fill(_USER_INPUT_SEL, user, timeout=60_000)
    page.fill('input[type="password"]', password, timeout=30_000)
    page.click(
        'button[type="submit"], button:has-text("Login"), input[type="submit"]',
        timeout=30_000,
    )
    page.wait_for_load_state("networkidle", timeout=120_000)
    page.locator("text=Constructionary").first.click()
    page.wait_for_timeout(4000)


def get_context(page: Page):
    for marker in ["FOLDER EXPLORER", "Constructionary", "FILTER BY"]:
        if page.locator(f"text={marker}").count() > 0:
            return page
    for frame in page.frames:
        if not frame.url:
            continue
        try:
            for marker in ["FOLDER EXPLORER", "Constructionary", "FILTER BY"]:
                if frame.locator(f"text={marker}").count() > 0:
                    return frame
        except Exception:
            continue
    return page


def parse_entity_id_from_payload(post_data: str) -> Optional[str]:
    """GWT-RPC v7 format: 7|flags|N|s1|s2|...|sN|stream
    The category entity ID is the last digit-only string in the string table."""
    parts = post_data.split("|")
    try:
        n = int(parts[2])
        strings = parts[3: 3 + n]
        for s in reversed(strings):
            if s.strip().isdigit():
                return s.strip()
    except (ValueError, IndexError):
        pass
    return None


# Candidate selectors for the scrollable parts/details panel (right side).
# Ordered from most specific to most generic.
_PARTS_PANEL_SELECTORS = [
    ".ConstructionaryDetailsPanel",
    ".partsSectionBorderDark",
    "[class*='detailsPanel']",
    "[class*='DetailsPanel']",
    "[class*='rightPanel']",
    "[class*='RightPanel']",
    "[class*='partsPanel']",
    "[class*='PartsPanel']",
    "[class*='contentPanel']",
    "[class*='ContentPanel']",
]


def _build_parts_scroll_js(delta_y: int = 1800) -> str:
    """Return a JS snippet that scrolls the first matching parts panel by delta_y px."""
    selectors_js = json.dumps(_PARTS_PANEL_SELECTORS)
    return f"""
(function() {{
    var selectors = {selectors_js};
    for (var i = 0; i < selectors.length; i++) {{
        var el = document.querySelector(selectors[i]);
        if (el) {{
            el.scrollBy(0, {delta_y});
            return selectors[i];
        }}
    }}
    // No panel found — scroll the largest scrollable div as fallback
    var divs = Array.from(document.querySelectorAll('div'));
    var best = null, bestH = 0;
    divs.forEach(function(d) {{
        if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > bestH) {{
            // Exclude the left tree panel (narrow elements)
            var rect = d.getBoundingClientRect();
            if (rect.left > 200) {{
                best = d;
                bestH = d.clientHeight;
            }}
        }}
    }});
    if (best) {{ best.scrollBy(0, {delta_y}); return 'auto:' + best.className.slice(0,40); }}
    return null;
}})()
"""


_PARTS_PER_RPC = 10          # estimated parts returned per GWT page
_BASE_SCROLL_ROUNDS = 30     # minimum scroll rounds for any leaf
_MAX_SCROLL_ROUNDS = 800     # hard cap (auto-exit via idle detection)
_IDLE_ROUNDS_THRESHOLD = 8   # stop after this many consecutive rounds with no new RPC


def is_leaf_by_name(name: str) -> bool:
    low = name.lower()
    has_categories = bool(re.search(r"\d+\s+categor", low))
    has_specs = bool(re.search(r"\d+\s+specification", low))
    has_parts = bool(re.search(r"\d+\s+parts?", low))
    if has_categories:
        return False
    if has_specs or has_parts:
        return True
    return False


def parse_expected_parts(name: str) -> int:
    """Extract the advertised part count from a leaf category tile name."""
    m = re.search(r"(\d+)\s+parts?", name, re.I)
    return int(m.group(1)) if m else 0


def adaptive_scroll_rounds(expected_parts: int, base: int = _BASE_SCROLL_ROUNDS) -> int:
    """Return scroll rounds needed to load all pages for a category."""
    if expected_parts <= 0:
        return base
    needed = max(base, expected_parts // _PARTS_PER_RPC + 10)
    return min(needed, _MAX_SCROLL_ROUNDS)


def walk_and_trigger_rpc(
    ctx,
    tree_sel: str,
    max_folders: int,
    scroll_rounds: int,
    seq: Dict[str, Any],
    folder_start: int = 0,
    folder_limit: int = 0,
) -> None:
    """Click folder tiles and scroll in leaf folders to trigger getPartDetails RPCs.

    Args:
        folder_start: Skip the first N leaf categories (0 = start from beginning).
        folder_limit: Stop after processing this many leaf categories (0 = no limit).
        scroll_rounds: Base scroll rounds; overridden per-category by adaptive logic.
    """
    visited: set = set()
    i = 0
    leaf_count = 0          # counts only leaf categories processed
    under_extracted = []    # categories where we likely missed pages

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
        label = "LEAF" if is_leaf else "FOLDER"

        if not is_leaf:
            seq["current_folder"] = name
            seq["current_category"] = ""
            print(f"\n[node {i}/{live_count}+] [{label}] {name}")
        else:
            seq["current_category"] = name

            # Batch mode: skip leaves before folder_start
            if leaf_count < folder_start:
                leaf_count += 1
                print(f"\n[node {i}/{live_count}+] [SKIP leaf {leaf_count}/{folder_start}] {name}")
                continue

            print(f"\n[node {i}/{live_count}+] [LEAF #{leaf_count + 1}] {name}")

        try:
            folder.click(timeout=8000)
        except Exception as e:
            print(f"  [skip] click failed: {e}")
            continue

        try:
            ctx.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        ctx.wait_for_timeout(1200)

        if not is_leaf:
            continue

        leaf_count += 1

        # ── Scroll the parts panel to trigger pagination RPCs ─────────────────
        # mouse.wheel(0, 0) scrolls the folder tree — use JS to target the right panel.
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

        # ── Adaptive rounds based on expected part count ───────────────────────
        expected = parse_expected_parts(name)
        rounds = adaptive_scroll_rounds(expected, base=scroll_rounds)
        if expected > 0:
            print(f"  [scroll] expected {expected} parts → {rounds} scroll rounds")

        prev_rpc_count = seq["n"]
        idle_rounds = 0
        last_rpc_at_round = seq["n"]

        for _ in range(rounds):
            ctx.wait_for_timeout(1200)
            if scrolled_via_js and matched_sel:
                try:
                    ctx.evaluate(scroll_js)
                except Exception:
                    ctx.mouse.move(900, 450)
                    ctx.mouse.wheel(0, 1800)
            else:
                ctx.mouse.wheel(0, 1800)

            if seq["n"] == last_rpc_at_round:
                idle_rounds += 1
                if idle_rounds >= _IDLE_ROUNDS_THRESHOLD:
                    break
            else:
                idle_rounds = 0
                last_rpc_at_round = seq["n"]

        new_rpcs = seq["n"] - prev_rpc_count
        estimated_parts = new_rpcs * _PARTS_PER_RPC
        print(f"  [scroll] {new_rpcs} RPC(s) → ~{estimated_parts} parts")

        # ── Verify coverage ────────────────────────────────────────────────────
        if expected > 0 and estimated_parts < expected * 0.5:
            msg = (
                f"  [WARNING] Under-extracted: ~{estimated_parts} of {expected} "
                f"expected parts ({new_rpcs} RPCs). Scroll may not be reaching panel."
            )
            print(msg)
            under_extracted.append({
                "category": name,
                "expected": expected,
                "rpcs_fired": new_rpcs,
                "estimated_parts": estimated_parts,
            })

        # ── Batch limit check ──────────────────────────────────────────────────
        if folder_limit > 0 and leaf_count >= folder_start + folder_limit:
            print(f"\n[collector] Batch limit reached ({folder_limit} leaves). Stopping.")
            break

    # ── End-of-run summary ─────────────────────────────────────────────────────
    print(f"\n[collector] Traversal complete. Leaves processed: {leaf_count}")
    if under_extracted:
        print(f"[collector] ⚠  {len(under_extracted)} categories likely under-extracted:")
        for u in under_extracted:
            print(f"  • {u['category'][:70]}")
            print(f"    expected={u['expected']}  rpcs={u['rpcs_fired']}  ~parts={u['estimated_parts']}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Capture Promasch GWT getPartDetails payloads")
    p.add_argument("--base-url", default="https://gw.promasch.in")
    p.add_argument("--user", default=os.getenv("CONSTRUCTIONARY_USER", ""))
    p.add_argument("--password", default=os.getenv("CONSTRUCTIONARY_PASSWORD", ""))
    p.add_argument("--headful", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None, help="Defaults to api-extraction/data/<run_id>")
    p.add_argument("--max-folders", type=int, default=5000)
    p.add_argument("--scroll-rounds", type=int, default=15, help="Base scroll rounds (adaptive per category)")
    p.add_argument("--folder-start", type=int, default=0, help="Skip first N leaf categories")
    p.add_argument("--folder-limit", type=int, default=0, help="Process at most N leaf categories (0=all)")
    p.add_argument("--sel-tree", default=None)
    return p.parse_args()


def ensure_credentials(user: str, password: str) -> None:
    if not user or not password:
        raise ValueError(
            "Missing credentials. Pass --user/--password or set "
            "CONSTRUCTIONARY_USER / CONSTRUCTIONARY_PASSWORD."
        )


def run_collection(
    output_dir: Path,
    base_url: str,
    user: str,
    password: str,
    headful: bool,
    max_folders: int,
    scroll_rounds: int,
    tree_sel_override: Optional[str],
    folder_start: int = 0,
    folder_limit: int = 0,
) -> List[Dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads_dir = output_dir / "payloads"
    dumps_dir = output_dir / "dumps"
    logs_dir = output_dir / "logs"
    for d in (payloads_dir, dumps_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    catalog: List[Dict[str, Any]] = []
    seq = {"n": 0}

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
        catalog.append(
            {
                "dump": str(dump_path.relative_to(output_dir)),
                "payload": str(payload_path.relative_to(output_dir)),
                "captured_at_ms": int(time.time() * 1000),
                "url": response.url,
                "entity_id": parse_entity_id_from_payload(pd),
                "folder": seq.get("current_folder", ""),
                "category": seq.get("current_category", ""),
            }
        )
        print(f"  [captured] getPartDetails → {stem}.txt")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()
        page.on("response", on_response)

        login_and_open_constructionary(page, base_url, user, password)
        ctx = get_context(page)

        tree_sel = tree_sel_override
        if not tree_sel:
            _, tree_sel = first_match(ctx, _TREE_SELECTORS)
        if not tree_sel:
            print("[collector] No tree selector matched; saving snapshot hint only")
            (output_dir / "logs" / "no_tree_selector.txt").write_text(
                "No folder tree selector matched. Re-run with --sel-tree from UI-extraction probe.\n",
                encoding="utf-8",
            )
        else:
            print(f"[collector] Using tree selector: {tree_sel}")
            walk_and_trigger_rpc(
                ctx, tree_sel, max_folders, scroll_rounds, seq,
                folder_start=folder_start,
                folder_limit=folder_limit,
            )

        auth_path = output_dir / "auth_state.json"
        context.storage_state(path=str(auth_path))
        print(f"[collector] Saved auth state → {auth_path}")

        browser.close()

    catalog_path = output_dir / "payload_catalog.json"
    catalog_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[collector] Catalog entries: {len(catalog)} → {catalog_path}")
    return catalog


def main() -> None:
    args = parse_args()
    ensure_credentials(args.user, args.password)
    root = Path(__file__).resolve().parent
    out = args.output_dir or (root / "data" / str(int(time.time())))
    run_collection(
        output_dir=out.resolve(),
        base_url=args.base_url,
        user=args.user,
        password=args.password,
        headful=args.headful,
        max_folders=args.max_folders,
        scroll_rounds=args.scroll_rounds,
        tree_sel_override=args.sel_tree,
        folder_start=args.folder_start,
        folder_limit=args.folder_limit,
    )


if __name__ == "__main__":
    main()
