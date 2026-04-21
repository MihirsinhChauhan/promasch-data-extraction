"""
Orders PO Collector: navigate to Orders > PO > Completed, intercept GWT
order-list API, paginate via HTTP, extract all PO numbers.

Two-phase approach:
  Phase 1 – Playwright: login, navigate, capture initial order-list API + auth
  Phase 2 – HTTP:       paginate order-list API, regex-extract PO numbers

Saves:
  po_catalog.json       – All PO entries with order_id + pdf_url
  auth_state.json       – Browser cookies for API replay
  payloads/*.txt        – Raw GWT POST bodies
  dumps/*.txt           – Raw GWT responses

Probe snapshots (each phase saves selector_probe_<name>.json):
  snapshot_login         – post-login HOME DASHBOARD (before any nav)
  snapshot_post_purchase – after clicking the Purchase top-menu tab
  snapshot_orders_list   – after clicking Orders PO Completed

Usage:
  uv run python collector.py --user USER --password PASS [--headful]
  uv run python collector.py --snapshot-ui --snapshot-ui-only --headful
  uv run python collector.py --snapshot-ui --snapshot-post-purchase --headful
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from playwright.sync_api import Frame, Page, sync_playwright

from ui_probe import (
    ORDERS_COMPLETED_SELECTORS,
    PURCHASE_NAV_SELECTORS,
    all_probe_selectors,
    first_match,
    get_orders_ui_context,
    gwt_soft_wait_after_action,
    load_selectors_config,
    merge_selectors,
    owning_page,
    post_purchase_probe_selectors,
    save_selector_snapshot,
    wait_for_post_login_shell,
)

# ── Endpoints ────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://gw.promasch.in"
DEFAULT_ERP_URL = "https://gw.promasch.in/deptherp/erp"

# ── PDF URL template ─────────────────────────────────────────────────────────

PDF_URL_TEMPLATE = (
    "https://gw.promasch.in/OrderPdf?"
    "orderType=PO1&orderId={order_id}&Specs=true&Location=true&showPrice=1"
)

# ── PO number regex ──────────────────────────────────────────────────────────
# Matches: PO(INI)E-HR-HR-21V-V49-MH-A-1661/11-04-2026/12625
# Captures the last numeric segment (order_id).

_PO_RE = re.compile(
    r'PO\([^)]+\)'       # PO(INI) or PO(XXX)
    r'[^"\s|,\]\[)]+?'   # middle segments (entity, codes, etc.)
    r'/\d{2}-\d{2}-\d{4}'  # /DD-MM-YYYY date
    r'/(\d+)'             # /ORDER_ID
)

# ── Login selectors ──────────────────────────────────────────────────────────

_USER_INPUT_SEL = (
    'input[type="text"], input[type="email"], input[name*="user"], '
    'input[name*="email"], input[id*="user"], input[id*="email"], '
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"])'
    ':not([type="checkbox"])'
)


# ── Login ────────────────────────────────────────────────────────────────────

def login_and_wait(page: Page, base_url: str, user: str, password: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector(_USER_INPUT_SEL, timeout=60_000)
    page.fill(_USER_INPUT_SEL, user, timeout=60_000)
    page.fill('input[type="password"]', password, timeout=30_000)
    page.click(
        'button[type="submit"], button:has-text("Login"), input[type="submit"]',
        timeout=30_000,
    )
    page.wait_for_load_state("networkidle", timeout=120_000)
    print("[collector] Login complete.")


# ── Navigation ───────────────────────────────────────────────────────────────
#
# Context:
#   After login the user sees the HOME DASHBOARD — the Purchase-section sidebar
#   (with RFOs / INDENTS / ORDERS tiles) is NOT yet visible.
#
#   Clicking the top-menu "Purchase | Work Order | Challans | Vendor Bills" tab
#   loads the overview page which has a LEFT SIDEBAR with:
#       RFOs | Vendor Comparisons | INDENTS | ORDERS | Vendor Bills
#   The ORDERS sidebar tile contains two sub-sections: PO and WO.
#   Each sub-section shows RAISED / IN-PROCESS / COMPLETED counts.
#
#   Our goal:  click "COMPLETED" inside the PO sub-section of ORDERS.
#
# Strategy order (most specific → most permissive):
#   A. Playwright selector: sidebar PO card → COMPLETED text
#   B. JS DOM walk: anchor on "ORDERS" heading, descend into PO block, click COMPLETED
#   C. Dropdown approach: set top-bar <select> dropdowns to Orders > PO > Completed
#   D. Manual fallback (headful only)


# JS: use the actual CSS classes observed in snapshot_post_purchase.html.
#   div.INDENTS  → section headings (both INDENTS and ORDERS share this class)
#   div.POName   → PO / WO sub-section headings
#   div.in_process → RAISED / IN-PROCESS / COMPLETED row labels
#
# Strategy: find div.POName whose text is "PO", walk up to its row group,
# then find the first div.in_process containing "COMPLETED" inside that group.
# This reliably picks PO COMPLETED and not WO COMPLETED.
_ORDERS_PO_COMPLETED_JS = """
(function() {
    function isVisible(el) {
        return !!(el && el.offsetParent !== null);
    }
    function ownText(el) {
        var t = '';
        for (var i = 0; i < el.childNodes.length; i++) {
            if (el.childNodes[i].nodeType === 3) t += el.childNodes[i].textContent;
        }
        return t.trim();
    }

    // Strategy A: CSS-class based (most reliable from snapshot analysis)
    // Find PO heading via class .POName, walk up to its containing block,
    // then find the .in_process div with COMPLETED inside that block.
    var poHeaders = Array.from(document.querySelectorAll('div.POName'));
    for (var i = 0; i < poHeaders.length; i++) {
        var ph = poHeaders[i];
        if (!isVisible(ph)) continue;
        var ptxt = ownText(ph).trim();
        if (ptxt.toUpperCase() !== 'PO') continue;

        // Walk up from the PO heading to a block that contains COMPLETED rows
        var block = ph.parentElement;
        for (var d = 0; d < 8 && block; d++) {
            var bt = (block.textContent || '').toUpperCase();
            if (bt.indexOf('COMPLETED') >= 0 && bt.indexOf('RAISED') >= 0) break;
            block = block.parentElement;
        }
        if (!block) continue;

        // Find WO heading below PO heading (to exclude its sub-tree)
        var woHeaders = Array.from(document.querySelectorAll('div.POName'));
        var woBlock = null;
        for (var w = 0; w < woHeaders.length; w++) {
            var wh = woHeaders[w];
            if (!isVisible(wh)) continue;
            if (ownText(wh).trim().toUpperCase() === 'WO') {
                woBlock = wh.parentElement;
                for (var wd = 0; wd < 8 && woBlock; wd++) {
                    var wbt = (woBlock.textContent || '').toUpperCase();
                    if (wbt.indexOf('COMPLETED') >= 0 && wbt.indexOf('RAISED') >= 0 && woBlock !== block) break;
                    woBlock = woBlock.parentElement;
                }
                break;
            }
        }

        // Click first .in_process containing COMPLETED in PO block, skip WO block
        var rows = Array.from(block.querySelectorAll('div.in_process, div.in_process.max_content'));
        for (var r = 0; r < rows.length; r++) {
            var row = rows[r];
            if (!isVisible(row)) continue;
            if (woBlock && woBlock !== block && woBlock.contains(row)) continue;
            var rtxt = (row.textContent || '').trim();
            if (/^COMPLETED/i.test(rtxt)) {
                // click the parent table row for better hit area
                var clickEl = row.closest('table') || row;
                clickEl.click();
                return JSON.stringify({ok: true, text: rtxt, strategy: 'css_class_po_completed', cls: row.className});
            }
        }
    }

    // Strategy B: ownText ORDERS heading → card → first PO COMPLETED
    var allEls = Array.from(document.querySelectorAll('*'));
    var ordersHeader = null;
    for (var j = 0; j < allEls.length; j++) {
        if (ownText(allEls[j]).toUpperCase().trim() === 'ORDERS' && isVisible(allEls[j])) {
            ordersHeader = allEls[j]; break;
        }
    }
    if (!ordersHeader) return JSON.stringify({ok: false, error: 'ORDERS_HEADING_NOT_FOUND'});

    var card = ordersHeader;
    for (var cd = 0; cd < 12 && card; cd++) {
        var ct = (card.textContent || '').toUpperCase();
        if (ct.indexOf('PO') >= 0 && ct.indexOf('WO') >= 0 &&
            ct.indexOf('COMPLETED') >= 0 && ct.indexOf('RAISED') >= 0) break;
        card = card.parentElement;
    }
    if (!card) return JSON.stringify({ok: false, error: 'ORDERS_CARD_NOT_FOUND'});

    // Find WO in card to exclude
    var cardEls = Array.from(card.querySelectorAll('*'));
    var woEl = null;
    for (var k = 0; k < cardEls.length; k++) {
        if (ownText(cardEls[k]).toUpperCase().trim() === 'WO' && isVisible(cardEls[k])) {
            woEl = cardEls[k]; break;
        }
    }
    var woTree = woEl ? woEl.parentElement : null;
    if (woTree) {
        for (var we = 0; we < 8 && woTree; we++) {
            var wt2 = (woTree.textContent || '').toUpperCase();
            if (wt2.indexOf('COMPLETED') >= 0 && wt2.indexOf('RAISED') >= 0 && woTree !== card) break;
            woTree = woTree.parentElement;
        }
    }

    for (var n = 0; n < cardEls.length; n++) {
        var cel = cardEls[n];
        if (!isVisible(cel)) continue;
        if (woTree && woTree.contains(cel)) continue;
        var ctxt2 = (cel.textContent || '').trim();
        if (/^COMPLETED(\\s*[|:·\\-]\\s*\\d+)?$/i.test(ctxt2) ||
            /^COMPLETED\\s*\\|\\s*\\d+/i.test(ctxt2)) {
            cel.click();
            return JSON.stringify({ok: true, text: ctxt2, strategy: 'orders_card_completed_no_wo'});
        }
    }

    return JSON.stringify({ok: false, error: 'COMPLETED_NOT_FOUND',
                           cardClasses: (card.className||'').slice(0,80)});
})()
"""


def _click_purchase_tab(
    ctx: Union[Page, Frame],
    purchase_selectors: List[str],
) -> bool:
    """Click the Purchase top-menu tab.  Returns True on success."""
    loc, sel_used = first_match(ctx, purchase_selectors)
    if loc is None:
        print("[collector] WARNING: Purchase menu tab not found.")
        return False
    try:
        loc.first.click(timeout=10_000)
        gwt_soft_wait_after_action(ctx, settle_ms=4000)
        print(f"[collector] Purchase tab clicked via: {sel_used!r}")
        return True
    except Exception as e:
        print(f"[collector] Purchase tab click failed ({sel_used!r}): {e}")
        return False


def _click_orders_po_completed_js(ctx: Union[Page, Frame]) -> bool:
    """Run JS DOM walk to click COMPLETED inside ORDERS > PO sidebar block."""
    try:
        raw = ctx.evaluate(_ORDERS_PO_COMPLETED_JS)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if result.get("ok"):
            gwt_soft_wait_after_action(ctx)
            print(
                f"[collector] COMPLETED clicked via JS: {result.get('text', '')!r} "
                f"tag={result.get('tag','')} strategy={result.get('strategy', '')}"
            )
            return True
        print(f"[collector] JS nav error: {result.get('error')} | {result.get('cardText','')[:120]}")
    except Exception as e:
        print(f"[collector] JS navigation threw: {e}")
    return False


def _set_dropdown_filters(ctx: Union[Page, Frame]) -> bool:
    """Fallback: set <select> dropdowns at top of list to Orders > PO > Completed."""
    try:
        selects = ctx.locator("select")
        if selects.count() < 1:
            return False

        for label in ("Orders",):
            for idx in range(selects.count()):
                dd = selects.nth(idx)
                try:
                    dd.select_option(label=label, timeout=2000)
                    print(f"[collector] Dropdown {idx} → {label!r}")
                    break
                except Exception:
                    pass

        gwt_soft_wait_after_action(ctx, settle_ms=1500)
        selects = ctx.locator("select")

        for label in ("PO",):
            for idx in range(1, selects.count()):
                dd = selects.nth(idx)
                try:
                    dd.select_option(label=label, timeout=2000)
                    print(f"[collector] Dropdown {idx} → {label!r}")
                    break
                except Exception:
                    pass

        gwt_soft_wait_after_action(ctx, settle_ms=1500)
        selects = ctx.locator("select")

        for idx in range(selects.count() - 1, -1, -1):
            dd = selects.nth(idx)
            try:
                dd.select_option(label="Completed", timeout=2000)
                print(f"[collector] Dropdown {idx} → 'Completed'")
                break
            except Exception:
                pass

        gwt_soft_wait_after_action(ctx, settle_ms=2500)
        return True
    except Exception as e:
        print(f"[collector] Dropdown approach failed: {e}")
        return False


def navigate_to_orders_completed(
    ctx: Union[Page, Frame],
    purchase_selectors: List[str],
    completed_selectors: List[str],
    *,
    output_dir: Optional[Path] = None,
    snapshot_post_purchase: bool = False,
) -> bool:
    """Navigate to Orders > PO > Completed.

    Step 1 – Click the Purchase top-menu tab (loads sidebar with ORDERS tile).
    Step 2 – Optional post-purchase snapshot.
    Step 3 – JS DOM walk anchored on ORDERS heading → PO block → COMPLETED.
    Step 4 – Fallback: Playwright selectors for ORDERS COMPLETED.
    Step 5 – Fallback: dropdown <select> filters.
    """
    # ── Step 1: click the Purchase tab ────────────────────────────────────────
    if not _click_purchase_tab(ctx, purchase_selectors):
        print("[collector] Cannot click Purchase tab; aborting navigation.")
        return False

    # ── Step 2: post-purchase snapshot ────────────────────────────────────────
    if snapshot_post_purchase and output_dir is not None:
        save_selector_snapshot(
            ctx, output_dir, "post_purchase", post_purchase_probe_selectors()
        )
        print(
            f"[collector] Snapshot (post_purchase) → "
            f"{output_dir}/selector_probe_post_purchase.json"
        )

    # ── Step 3: JS walk inside the sidebar ORDERS > PO block ─────────────────
    if _click_orders_po_completed_js(ctx):
        if _ensure_orders_list(ctx):
            return True
        print("[collector] JS click succeeded but list not confirmed, continuing...")

    # ── Step 4: Playwright selector fallback ──────────────────────────────────
    for sel in completed_selectors:
        try:
            cloc = ctx.locator(sel).first
            if cloc.count() > 0:
                cloc.click(timeout=10_000)
                gwt_soft_wait_after_action(ctx)
                print(f"[collector] COMPLETED clicked via selector fallback: {sel!r}")
                if _ensure_orders_list(ctx):
                    return True
        except Exception:
            continue

    # ── Step 5: dropdown filter fallback ──────────────────────────────────────
    if _set_dropdown_filters(ctx):
        if _ensure_orders_list(ctx):
            print("[collector] Navigated via dropdown filters.")
            return True

    print(
        "[collector] WARNING: Could not navigate to Orders > PO > Completed. "
        "Run with --headful to navigate manually."
    )
    return False


def _ensure_orders_list(ctx: Union[Page, Frame]) -> bool:
    """Verify the Orders PO Completed list is showing."""
    checks = [
        "text=/PO NO\\./i",
        "text=Search Order Number here",
        "text=/ORDER VALUE STATUS/i",
    ]
    for _ in range(20):
        for sel in checks:
            try:
                if ctx.locator(sel).first.count() > 0:
                    return True
            except Exception:
                continue
        ctx.wait_for_timeout(500)
    return False


# ── Scroll helpers ────────────────────────────────────────────────────────────

_SCROLL_LIST_JS = """
(function(delta) {
    // Find the main list container: tallest scrollable div that's not the sidebar
    var divs = Array.from(document.querySelectorAll('div'));
    var best = null, bestScore = 0;
    divs.forEach(function(d) {
        if (d.scrollHeight <= d.clientHeight + 50) return;
        var rect = d.getBoundingClientRect();
        // Skip narrow sidebar panels (< 300px wide) and elements too far left
        if (rect.width < 300 || rect.left < 150) return;
        var score = d.scrollHeight - d.clientHeight;
        if (score > bestScore) { best = d; bestScore = score; }
    });
    if (best) {
        best.scrollBy(0, delta);
        return best.className ? best.className.slice(0, 80) : 'unnamed';
    }
    window.scrollBy(0, delta);
    return 'window_fallback';
})(arguments[0])
"""

_SCROLL_ALL_JS = """
(function(delta) {
    var count = 0;
    Array.from(document.querySelectorAll('div')).forEach(function(d) {
        if (d.scrollHeight <= d.clientHeight + 50) return;
        var rect = d.getBoundingClientRect();
        if (rect.width >= 250 && rect.height >= 120 && rect.left >= 120) {
            d.scrollBy(0, delta);
            count++;
        }
    });
    if (count === 0) window.scrollBy(0, delta);
    return count;
})(arguments[0])
"""


def _scroll_orders_list(
    ctx: Union[Page, Frame],
    seq: Dict[str, int],
    *,
    max_rounds: int = 150,
    idle_threshold: int = 5,
    scroll_delta: int = 800,
) -> int:
    """Scroll the orders list to trigger lazy-loading API calls.

    Returns the number of new API responses triggered.
    Each scroll fires a new page of the orders list (typically ~100 orders).
    With 11733 orders we need ~117 pages → ~117 scroll rounds minimum.
    """
    pw = owning_page(ctx)
    prev_n = seq["n"]
    idle_count = 0
    last_n = seq["n"]

    for round_num in range(max_rounds):
        try:
            panel = ctx.evaluate(_SCROLL_LIST_JS, scroll_delta)
            if round_num == 0:
                print(f"[collector] Scrolling list panel: {panel!r}")
            # Every 3 rounds, also scroll all scrollable containers
            if round_num % 3 == 2:
                extra = ctx.evaluate(_SCROLL_ALL_JS, scroll_delta)
                if extra and round_num < 10:
                    print(f"[collector] Broad-scroll pass: {extra} container(s)")
        except Exception:
            try:
                pw.mouse.move(800, 450)
                pw.mouse.wheel(0, scroll_delta)
            except Exception:
                pass

        ctx.wait_for_timeout(1200)

        current_n = seq["n"]
        if current_n == last_n:
            idle_count += 1
            if idle_count >= idle_threshold:
                print(
                    f"[collector] Scroll idle for {idle_threshold} rounds, stopping. "
                    f"round={round_num}"
                )
                break
        else:
            idle_count = 0
            last_n = current_n
            if round_num % 20 == 0 or current_n - prev_n in (1, 10, 50, 100):
                print(
                    f"[collector] Scroll round {round_num}: "
                    f"{current_n - prev_n} API calls captured so far"
                )

    new_calls = seq["n"] - prev_n
    print(f"[collector] Scrolling done: {new_calls} new order-list API call(s)")
    return new_calls


# ── PO number extraction ────────────────────────────────────────────────────

def extract_po_numbers_from_text(text: str) -> List[Dict[str, str]]:
    """Extract PO numbers and order IDs from GWT response or DOM text.

    Returns list of {po_number, order_id, pdf_url}.
    """
    entries: List[Dict[str, str]] = []
    seen_ids: set[str] = set()
    for match in _PO_RE.finditer(text):
        order_id = match.group(1)
        if order_id in seen_ids:
            continue
        seen_ids.add(order_id)
        po_number = match.group(0)
        entries.append({
            "po_number": po_number,
            "order_id": order_id,
            "pdf_url": PDF_URL_TEMPLATE.format(order_id=order_id),
        })
    return entries


# ── GWT payload helpers ──────────────────────────────────────────────────────

def _parse_gwt_headers(post_data: str) -> Dict[str, str]:
    """Extract base_url, permutation, service, method from GWT v7 payload."""
    parts = post_data.split("|")
    try:
        n = int(parts[2])
        strings = parts[3 : 3 + n]
        return {
            "base_url": strings[0] if len(strings) > 0 else "",
            "permutation": strings[1] if len(strings) > 1 else "",
            "service": strings[2] if len(strings) > 2 else "",
            "method": strings[3] if len(strings) > 3 else "",
        }
    except (ValueError, IndexError):
        return {}


def _build_paginated_payload(
    template_payload: str,
    offset: int,
    page_size: int,
) -> Optional[str]:
    """Build a paginated payload by replacing the last two integer fields.

    GWT-RPC list payloads end with ...|page_size|offset| — we swap those.
    """
    parts = template_payload.rstrip("|").split("|")
    if len(parts) < 3:
        return None

    replaced = 0
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lstrip("-").isdigit():
            if replaced == 0:
                parts[i] = str(offset)
                replaced += 1
            elif replaced == 1:
                parts[i] = str(page_size)
                replaced += 1
                break

    if replaced < 2:
        return None

    return "|".join(parts) + "|"


# ── Collection ───────────────────────────────────────────────────────────────

def run_collection(
    output_dir: Path,
    base_url: str,
    user: str,
    password: str,
    headful: bool,
    wait_seconds: int = 60,
    page_size: int = 100,
    *,
    snapshot_ui: bool = False,
    snapshot_ui_manual: bool = False,
    snapshot_ui_only: bool = False,
    snapshot_post_purchase: bool = False,
) -> List[Dict[str, Any]]:
    """
    Phase 1 – Playwright: login, navigate to Orders PO Completed, capture API.
    Phase 2 – HTTP: paginate order list to extract all PO numbers.

    Returns the raw capture catalog (payloads/dumps).
    The main artefact is po_catalog.json written at the end.
    """
    snap_login = snapshot_ui or snapshot_ui_only

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads_dir = output_dir / "payloads"
    dumps_dir = output_dir / "dumps"
    logs_dir = output_dir / "logs"
    for d in (payloads_dir, dumps_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    catalog: List[Dict[str, Any]] = []
    seq = {"n": 0}
    list_payload_template: Dict[str, str] = {"value": ""}
    list_headers: Dict[str, str] = {}

    def on_response(response) -> None:
        """Context-level listener: capture every /deptherp/erp POST with PO data."""
        try:
            req = response.request
            if req.method != "POST":
                return
            url = req.url
            if "/deptherp/erp" not in url:
                return
            pd = req.post_data
            if not pd:
                return
            if response.status != 200:
                return
            body = response.text()
            if not body.strip().startswith("//OK"):
                return
        except Exception:
            return

        po_entries = extract_po_numbers_from_text(body)
        hdrs = _parse_gwt_headers(pd)
        method = hdrs.get("method", "unknown")

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        payload_path = payloads_dir / f"{stem}.txt"
        dump_path = dumps_dir / f"{stem}.txt"
        payload_path.write_text(pd, encoding="utf-8")
        dump_path.write_text(body, encoding="utf-8")

        entry: Dict[str, Any] = {
            "dump": str(dump_path.relative_to(output_dir)),
            "payload": str(payload_path.relative_to(output_dir)),
            "captured_at_ms": int(time.time() * 1000),
            "url": url,
            "method": method,
            "po_count": len(po_entries),
        }
        catalog.append(entry)

        # Use first response with PO data as pagination template.
        # Prefer later captures (from scroll) over early ones (from page load),
        # because scroll captures are more likely to be the actual order-list API.
        if po_entries:
            list_payload_template["value"] = pd
            list_headers.update(hdrs)
            if seq["n"] <= 5 or len(po_entries) >= 50:
                print(
                    f"  [captured] {method} → {stem}.txt  "
                    f"POs: {len(po_entries)}  (pagination template updated)"
                )
        elif method not in ("login",):
            if seq["n"] <= 10:
                print(
                    f"  [captured] {method} → {stem}.txt"
                )

    # ── Phase 1: Playwright ──────────────────────────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        context.on("response", on_response)
        page = context.new_page()

        login_and_wait(page, base_url, user, password)

        shell = wait_for_post_login_shell(page)
        if shell is not None:
            print("[collector] Post-login shell ready.")
            ctx: Union[Page, Frame] = shell
        else:
            print("[collector] WARNING: Post-login shell not detected.")
            ctx = get_orders_ui_context(page)
        page.wait_for_timeout(1500)

        sel_cfg = load_selectors_config(output_dir)
        purchase_sels = merge_selectors(
            PURCHASE_NAV_SELECTORS, sel_cfg, "purchase"
        )
        completed_sels = merge_selectors(
            ORDERS_COMPLETED_SELECTORS, sel_cfg, "completed"
        )

        if snap_login:
            save_selector_snapshot(
                ctx, output_dir, "login", all_probe_selectors()
            )
            print(
                f"[collector] UI snapshot (login) → "
                f"{output_dir}/selector_probe_login.json"
            )

        if snapshot_ui_manual:
            print(
                f"[collector] Manual navigation window: {wait_seconds}s ..."
            )
            page.wait_for_timeout(wait_seconds * 1000)
            ctx = get_orders_ui_context(page)
            save_selector_snapshot(
                ctx, output_dir, "post_nav", all_probe_selectors()
            )
            print(
                f"[collector] UI snapshot (post_nav) → "
                f"{output_dir}/selector_probe_post_nav.json"
            )

        if snapshot_ui_only:
            nav_ok = False
        else:
            nav_ok = navigate_to_orders_completed(
                ctx,
                purchase_sels,
                completed_sels,
                output_dir=output_dir,
                snapshot_post_purchase=snapshot_post_purchase or snapshot_ui,
            )

        if nav_ok:
            ctx = get_orders_ui_context(page)
            try:
                ctx.locator("text=/PO NO\\./i").first.wait_for(
                    state="visible", timeout=45_000
                )
            except Exception:
                pass

            if snapshot_ui:
                save_selector_snapshot(
                    ctx, output_dir, "orders_list", all_probe_selectors()
                )

            print("[collector] Scrolling orders list to trigger lazy-loading API calls...")
            _scroll_orders_list(
                ctx,
                seq,
                max_rounds=150,
                idle_threshold=5,
                scroll_delta=800,
            )

            # ── Browser-side pagination (Phase 1.5) ──────────────────────────
            # Run pagination from WITHIN the live browser context so the server
            # sees full session state. This avoids the server-side 200-record
            # limit that hits replayed HTTP requests from outside the browser.
            if list_payload_template["value"] and list_headers.get("permutation"):
                print("[collector] Starting browser-side pagination...")
                _run_browser_pagination(
                    page=page,
                    captured_payload=list_payload_template["value"],
                    captured_headers=list_headers,
                    output_dir=output_dir,
                    payloads_dir=payloads_dir,
                    dumps_dir=dumps_dir,
                    page_size=page_size,
                    seq=seq,
                    captured_offsets=_get_captured_offsets(
                        list_payload_template["value"], page_size
                    ),
                )
        else:
            if not snapshot_ui_only:
                print(
                    f"[collector] Navigation failed. Waiting {wait_seconds}s "
                    "for manual interaction..."
                )
                page.wait_for_timeout(wait_seconds * 1000)

        auth_path = output_dir / "auth_state.json"
        context.storage_state(path=str(auth_path))
        print(f"[collector] Auth saved → {auth_path}")
        browser.close()

    print(f"[collector] Phase 1 complete: {len(catalog)} capture(s).")

    # ── Phase 2: collect all POs from every dump file ────────────────────────
    # Scan the entire dumps dir so browser-pagination files (written directly,
    # not tracked in catalog) are also included.
    all_po_entries: List[Dict[str, str]] = []

    for dump_file in sorted(dumps_dir.glob("*.txt")):
        try:
            text = dump_file.read_text(encoding="utf-8")
            all_po_entries.extend(extract_po_numbers_from_text(text))
        except Exception:
            pass

    # HTTP fallback: only runs when no order-list API was captured at all
    # (i.e. browser pagination never started). If browser pagination ran but
    # still stopped early, the HTTP path is unlikely to do better since the
    # server enforces the same per-session limit outside the browser.
    if not list_payload_template["value"] or not list_headers.get("permutation"):
        print(
            "[collector] No order-list API captured; cannot paginate. "
            "Re-run with --headful and navigate manually if needed."
        )
    elif not nav_ok:
        # Navigation failed; browser pagination didn't run — try HTTP.
        extra = _run_order_pagination(
            captured_payload=list_payload_template["value"],
            captured_headers=list_headers,
            auth_path=output_dir / "auth_state.json",
            output_dir=output_dir,
            payloads_dir=payloads_dir,
            dumps_dir=dumps_dir,
            page_size=page_size,
            seq=seq,
        )
        all_po_entries.extend(extra)

    # Deduplicate
    seen: set[str] = set()
    deduped: List[Dict[str, str]] = []
    for e in all_po_entries:
        oid = e["order_id"]
        if oid not in seen:
            seen.add(oid)
            deduped.append(e)

    catalog_path = output_dir / "po_catalog.json"
    catalog_path.write_text(
        json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[collector] PO catalog: {len(deduped)} entries → {catalog_path}")

    # Save raw capture catalog too
    raw_catalog_path = output_dir / "payload_catalog.json"
    raw_catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return catalog


def _get_captured_offsets(captured_payload: str, page_size: int) -> set:
    """Return the set of offsets already covered by the captured payload."""
    offsets: set = {0}
    try:
        parts = captured_payload.rstrip("|").split("|")
        last_offset = int(parts[-1])
        offsets.add(last_offset)
    except Exception:
        pass
    return offsets


def _run_browser_pagination(
    *,
    page,
    captured_payload: str,
    captured_headers: Dict[str, str],
    output_dir: Path,
    payloads_dir: Path,
    dumps_dir: Path,
    page_size: int,
    seq: Dict[str, int],
    captured_offsets: set,
) -> List[Dict[str, str]]:
    """Paginate the order-list API from WITHIN the live browser context.

    Uses page.evaluate() to call fetch() so the browser's full session state
    (cookies, JSESSIONID, any server-side cursor) is preserved — bypassing the
    ~200-record limit observed when replaying requests via external HTTP.
    """
    erp_url = DEFAULT_ERP_URL
    origin = DEFAULT_BASE_URL
    permutation = captured_headers.get("permutation", "")

    all_entries: List[Dict[str, str]] = []
    offset = page_size
    consecutive_tiny = 0
    TINY_THRESHOLD = 200
    CONSECUTIVE_TINY_LIMIT = 3

    # Playwright evaluate() calls this function with the arg dict as its sole parameter.
    _FETCH_JS = """async (args) => {
    try {
        const resp = await fetch(args.url, {
            method: "POST",
            headers: {
                "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
                "Accept": "*/*",
                "X-GWT-Permutation": args.permutation,
                "Origin": args.origin
            },
            body: args.payload,
            credentials: "include"
        });
        const text = await resp.text();
        return {status: resp.status, body: text};
    } catch(e) {
        return {error: e.toString()};
    }
}"""

    print(
        f"[collector] Browser pagination (page_size={page_size}, "
        f"already have offsets: {sorted(captured_offsets)})..."
    )

    while True:
        if offset in captured_offsets:
            offset += page_size
            continue

        payload = _build_paginated_payload(captured_payload, offset, page_size)
        if payload is None:
            print("[collector] Cannot build paginated payload, stopping.")
            break

        try:
            result = page.evaluate(_FETCH_JS, {
                "url": erp_url,
                "permutation": permutation,
                "origin": origin,
                "payload": payload,
            })
        except Exception as e:
            print(f"[collector] Browser pagination JS error at offset={offset}: {e}")
            break

        if not isinstance(result, dict):
            print(f"[collector] Browser pagination unexpected result at offset={offset}: {result!r}")
            break

        if result.get("error"):
            print(f"[collector] Browser fetch error at offset={offset}: {result['error']}")
            break

        status = result.get("status", 0)
        body = result.get("body", "") or ""

        if status != 200 or not body.strip().startswith("//OK"):
            print(
                f"[collector] Browser pagination stopped: "
                f"status={status} at offset={offset}"
            )
            break

        body_len = len(body)

        if body_len < TINY_THRESHOLD:
            consecutive_tiny += 1
            print(
                f"  [browser-paginate] offset={offset} → tiny response "
                f"({body_len} chars), consecutive={consecutive_tiny}"
            )
            if consecutive_tiny >= CONSECUTIVE_TINY_LIMIT:
                print("[collector] Browser pagination: server returned no more data.")
                break
        else:
            consecutive_tiny = 0

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        (payloads_dir / f"{stem}.txt").write_text(payload, encoding="utf-8")
        (dumps_dir / f"{stem}.txt").write_text(body, encoding="utf-8")

        po_entries = extract_po_numbers_from_text(body)
        all_entries.extend(po_entries)
        captured_offsets.add(offset)

        total_so_far = len(all_entries)
        if offset % (page_size * 10) == 0 or len(po_entries) > 0:
            print(
                f"  [browser-paginate] offset={offset} → {len(po_entries)} PO(s) "
                f"(total so far: {total_so_far})"
            )

        offset += page_size

    print(f"[collector] Browser pagination done: {len(all_entries)} PO(s).")
    return all_entries


def _run_order_pagination(
    *,
    captured_payload: str,
    captured_headers: Dict[str, str],
    auth_path: Path,
    output_dir: Path,
    payloads_dir: Path,
    dumps_dir: Path,
    page_size: int,
    seq: Dict[str, int],
) -> List[Dict[str, str]]:
    """Paginate the order list API to extract all PO numbers."""
    import requests as req_lib

    permutation = captured_headers.get("permutation", "")
    base_url = captured_headers.get("base_url", "")

    if not permutation:
        print("[collector] Cannot paginate: no permutation captured.")
        return []

    if not auth_path.is_file():
        print("[collector] Cannot paginate: auth_state.json missing.")
        return []

    raw_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    cookies = {c["name"]: c["value"] for c in raw_auth.get("cookies", [])}

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    http_headers = {
        "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
        "Accept": "*/*",
        "Referer": base_url,
        "Origin": origin,
        "X-GWT-Permutation": permutation,
    }

    all_entries: List[Dict[str, str]] = []

    # Find offsets already captured by the Playwright scroll phase so we skip them
    captured_offsets: set[int] = set()
    try:
        parts = captured_payload.rstrip("|").split("|")
        last_offset = int(parts[-1])
        captured_offsets.add(last_offset)
    except Exception:
        pass
    captured_offsets.add(0)

    offset = page_size
    consecutive_tiny = 0
    # Stop only when server returns a truly empty //OK response (< 200 chars),
    # not just because the PO regex finds nothing (older records may lack PO refs).
    TINY_THRESHOLD = 200
    CONSECUTIVE_TINY_LIMIT = 3

    print(f"[collector] Starting HTTP pagination (page_size={page_size}, already have offsets: {sorted(captured_offsets)})...")

    while True:
        if offset in captured_offsets:
            offset += page_size
            continue

        payload = _build_paginated_payload(captured_payload, offset, page_size)
        if payload is None:
            print("[collector] Cannot build paginated payload, stopping.")
            break

        try:
            resp = req_lib.post(
                DEFAULT_ERP_URL,
                data=payload.encode("utf-8"),
                headers=http_headers,
                cookies=cookies,
                timeout=120.0,
            )
        except Exception as e:
            print(f"[collector] Pagination failed at offset={offset}: {e}")
            break

        if resp.status_code != 200 or not resp.text.strip().startswith("//OK"):
            print(
                f"[collector] Pagination stopped: "
                f"status={resp.status_code} at offset={offset}"
            )
            break

        body = resp.text
        body_len = len(body)

        # Tiny response = server has no more records
        if body_len < TINY_THRESHOLD:
            consecutive_tiny += 1
            print(
                f"  [paginate] offset={offset} → tiny response ({body_len} chars), "
                f"consecutive={consecutive_tiny}"
            )
            if consecutive_tiny >= CONSECUTIVE_TINY_LIMIT:
                print("[collector] Pagination: server returned no more data.")
                break
        else:
            consecutive_tiny = 0

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        (payloads_dir / f"{stem}.txt").write_text(payload, encoding="utf-8")
        (dumps_dir / f"{stem}.txt").write_text(body, encoding="utf-8")

        po_entries = extract_po_numbers_from_text(body)
        all_entries.extend(po_entries)

        total_so_far = len(all_entries)
        if offset % (page_size * 10) == 0 or len(po_entries) > 0:
            print(
                f"  [paginate] offset={offset} → {len(po_entries)} PO(s) "
                f"(total so far: {total_so_far})"
            )

        offset += page_size

    print(f"[collector] Pagination done: {len(all_entries)} PO(s) from HTTP.")
    return all_entries


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture Promasch Orders PO Completed – extract PO numbers"
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument(
        "--user",
        default=os.getenv(
            "ORDER_USER",
            os.getenv("INDENT_USER", os.getenv("CONSTRUCTIONARY_USER", "")),
        ),
    )
    p.add_argument(
        "--password",
        default=os.getenv(
            "ORDER_PASSWORD",
            os.getenv(
                "INDENT_PASSWORD",
                os.getenv("CONSTRUCTIONARY_PASSWORD", ""),
            ),
        ),
    )
    p.add_argument("--headful", action="store_true")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Data directory (default: api-extraction/orders/data/<ts>)",
    )
    p.add_argument(
        "--wait",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds to wait for API calls / manual interaction (default: 60)",
    )
    p.add_argument("--page-size", type=int, default=100)
    p.add_argument(
        "--snapshot-ui",
        action="store_true",
        help="Save snapshots at login + post_purchase + orders_list",
    )
    p.add_argument(
        "--snapshot-ui-manual",
        action="store_true",
        help="After login snapshot, wait --wait seconds then save post_nav probe",
    )
    p.add_argument(
        "--snapshot-ui-only",
        action="store_true",
        help="Login snapshot only, skip navigation (saves auth_state.json)",
    )
    p.add_argument(
        "--snapshot-post-purchase",
        action="store_true",
        help=(
            "Click Purchase tab, snapshot the Purchase overview page "
            "(shows real ORDERS sidebar), then proceed with navigation. "
            "Use after --snapshot-ui-only to map the sidebar selectors."
        ),
    )
    return p.parse_args()


def ensure_credentials(user: str, password: str) -> None:
    if not user or not password:
        raise SystemExit(
            "Missing credentials. Pass --user/--password or set "
            "ORDER_USER / ORDER_PASSWORD environment variables."
        )


def main() -> None:
    args = parse_args()
    ensure_credentials(args.user, args.password)

    root = Path(__file__).resolve().parent
    out = args.output_dir or (root / "data" / str(int(time.time())))
    out = out.resolve()

    run_collection(
        output_dir=out,
        base_url=args.base_url,
        user=args.user,
        password=args.password,
        headful=args.headful,
        wait_seconds=args.wait,
        page_size=args.page_size,
        snapshot_ui=args.snapshot_ui,
        snapshot_ui_manual=args.snapshot_ui_manual,
        snapshot_ui_only=args.snapshot_ui_only,
        snapshot_post_purchase=args.snapshot_post_purchase,
    )


if __name__ == "__main__":
    main()
