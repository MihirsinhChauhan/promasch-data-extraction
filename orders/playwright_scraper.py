"""
Orders PO scraper: navigate to Orders > PO > Completed, scroll the list, and
extract all PO entries.

Mirrors vendor-bills/playwright_scraper.py but adapted for the Orders list.

Why the old collector.py approach was incomplete
------------------------------------------------
The old code tried to paginate by POST-ing modified GWT-RPC payloads (swapping
the last two integer fields for offset / page_size).  That breaks because:

  1. GWT payload field order varies by method — the "last two ints" heuristic
     often replaces the wrong fields.
  2. The server enforces a session-cursor that rejects out-of-order offsets from
     external HTTP replays, capping results at ~200 records.

New approach (same as vendor-bills)
------------------------------------
  Phase 1  – Playwright scrolls the rendered list while a context-level response
             listener captures every GWT //OK reply that contains PO data.
             Both the DOM text AND the raw API bodies are parsed for PO entries.

  Phase 2  – Browser-side pagination: page.evaluate() runs fetch() from inside
             the live browser session so the server sees the real JSESSIONID /
             X-GWT-Permutation.  We iterate offsets from 0 → end, skipping
             offsets that were already captured during scrolling.

  Phase 3  – Aggregate: deduplicate all entries from DOM text + API responses +
             browser pagination, save po_catalog.json.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from playwright.sync_api import Error as PlaywrightError, Frame, Page, sync_playwright

import config
from utils import setup_logging, save_json, load_json

log = setup_logging("scraper")

# ── Endpoints ────────────────────────────────────────────────────────────────

_ERP_URL = "https://gw.promasch.in/deptherp/erp"

# ── PDF URL template ─────────────────────────────────────────────────────────

PDF_URL_TEMPLATE = (
    "https://gw.promasch.in/OrderPdf?"
    "orderType=PO1&orderId={order_id}&Specs=true&Location=true&showPrice=1"
)

# ── PO number regex ──────────────────────────────────────────────────────────
# Matches: PO(INI)E-HR-HR-21V-V49-MH-A-1661/11-04-2026/12625
# Captures the trailing numeric order_id.

_PO_RE = re.compile(
    r'PO\([^)]+\)'           # PO(entity) prefix
    r'[^"\s|,\]\[)]+?'       # middle segments
    r'/\d{2}-\d{2}-\d{4}'    # /DD-MM-YYYY date
    r'/(\d+)'                # /ORDER_ID  ← captured group
)

# Splits the page text into one chunk per list row that starts with a PO entry.
# GWT renders each row as a block of text; we split on the PO( prefix.
_ROW_SPLIT = re.compile(r'(?=PO\([^)]+\))')

# Optional extra fields visible in the orders list (best-effort, may not match
# all UI variations).
_RE_VENDOR   = re.compile(r'PO\([^)]+\)[^\n]*?\n(.+?)(?=\n|\s{2,}|\d{2}-\d{2}-\d{4})', re.S)
_RE_AMOUNT   = re.compile(r'₹\s*([\d,]+(?:\.\d+)?)')
_RE_DATE     = re.compile(r'(\d{2}-\d{2}-\d{4})')

# ── Login selectors ──────────────────────────────────────────────────────────

_USER_INPUT_SEL = (
    'input[type="text"], input[type="email"], input[name*="user"], '
    'input[name*="email"], input[id*="user"], input[id*="email"], '
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"])'
    ':not([type="checkbox"])'
)

# ── Navigation JS (ORDERS > PO > COMPLETED) ──────────────────────────────────
# Anchors on .POName heading "PO", walks up to its container block, then clicks
# the first .in_process element whose text starts with "COMPLETED".

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

    // Strategy A: CSS-class based (.POName → .in_process COMPLETED)
    var poHeaders = Array.from(document.querySelectorAll('div.POName'));
    for (var i = 0; i < poHeaders.length; i++) {
        var ph = poHeaders[i];
        if (!isVisible(ph)) continue;
        if (ownText(ph).toUpperCase() !== 'PO') continue;

        var block = ph.parentElement;
        for (var d = 0; d < 8 && block; d++) {
            var bt = (block.textContent || '').toUpperCase();
            if (bt.indexOf('COMPLETED') >= 0 && bt.indexOf('RAISED') >= 0) break;
            block = block.parentElement;
        }
        if (!block) continue;

        // Exclude any WO sub-block inside the same card
        var woBlock = null;
        var woHeaders2 = Array.from(document.querySelectorAll('div.POName'));
        for (var w = 0; w < woHeaders2.length; w++) {
            var wh = woHeaders2[w];
            if (!isVisible(wh) || ownText(wh).toUpperCase() !== 'WO') continue;
            woBlock = wh.parentElement;
            for (var wd = 0; wd < 8 && woBlock; wd++) {
                var wbt = (woBlock.textContent || '').toUpperCase();
                if (wbt.indexOf('COMPLETED') >= 0 && wbt.indexOf('RAISED') >= 0 &&
                    woBlock !== block) break;
                woBlock = woBlock.parentElement;
            }
            break;
        }

        var rows = Array.from(block.querySelectorAll('div.in_process, div.in_process.max_content'));
        for (var r = 0; r < rows.length; r++) {
            var row = rows[r];
            if (!isVisible(row)) continue;
            if (woBlock && woBlock !== block && woBlock.contains(row)) continue;
            if (/^COMPLETED/i.test((row.textContent || '').trim())) {
                var clickEl = row.closest('table') || row;
                clickEl.click();
                return JSON.stringify({ok: true, strategy: 'css_class', text: row.textContent.trim().slice(0,60)});
            }
        }
    }

    // Strategy B: anchor on "ORDERS" heading
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
        if (/^COMPLETED(\\s*[|:·\\-]\\s*\\d+)?$/i.test(ctxt2)) {
            cel.click();
            return JSON.stringify({ok: true, strategy: 'orders_card', text: ctxt2});
        }
    }

    return JSON.stringify({ok: false, error: 'COMPLETED_NOT_FOUND'});
})()
"""

# ── JS scroll helpers ────────────────────────────────────────────────────────

_JS_FIND_SCROLLABLE = """() => {
    const candidates = document.querySelectorAll('div, td');
    let best = null, bestScore = 0;
    for (const el of candidates) {
        const style = getComputedStyle(el);
        const overY = style.overflowY;
        if (overY !== 'auto' && overY !== 'scroll') continue;
        if (el.scrollHeight <= el.clientHeight + 10) continue;
        const score = el.scrollHeight - el.clientHeight;
        if (score > bestScore) { bestScore = score; best = el; }
    }
    return best;
}"""

_JS_SCROLL_DOWN = """(el) => {
    el.scrollTop += 3000;
    return el.scrollTop;
}"""

# Fallback: scroll ALL wide, tall divs (handles multi-pane GWT layouts)
_JS_SCROLL_ALL = """(delta) => {
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
}"""

# ── Browser-side pagination JS ───────────────────────────────────────────────

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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _login(page: Page) -> None:
    log.info("Logging in as %s ...", config.LOGIN_USER)
    page.goto(config.BASE_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector(_USER_INPUT_SEL, timeout=60_000)
    page.fill(_USER_INPUT_SEL, config.LOGIN_USER)
    page.fill('input[type="password"]', config.LOGIN_PASSWORD)
    page.click(
        'button[type="submit"], button:has-text("Login"), input[type="submit"]',
        timeout=30_000,
    )
    page.wait_for_load_state("networkidle", timeout=120_000)
    log.info("Login complete")


def _get_frame_with_orders(page: Page) -> Union[Page, Frame]:
    """Return the frame (or page) that renders the orders list."""
    for frame in page.frames:
        try:
            if frame.locator("text=PO NO").count() > 0:
                return frame
            if frame.locator("text=Order").count() > 0:
                return frame
        except Exception:
            continue
    return page


def _navigate_to_purchase(ctx: Union[Page, Frame]) -> bool:
    """Click the Purchase top-menu tab."""
    selectors = [
        'div:has-text("Purchase"):not(:has(div)):not([style*="display: none"])',
        "text=Purchase",
        '[class*="menu"] >> text=Purchase',
        'td:has-text("Purchase")',
    ]
    for sel in selectors:
        try:
            loc = ctx.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=10_000)
                ctx.wait_for_timeout(4000)
                log.info("Purchase tab clicked via %r", sel)
                return True
        except Exception:
            continue
    log.warning("Purchase tab not found")
    return False


def _navigate_to_orders_completed(ctx: Union[Page, Frame]) -> bool:
    """Click ORDERS > PO > COMPLETED in the sidebar."""
    if not _navigate_to_purchase(ctx):
        return False

    # JS-based DOM walk (most reliable across GWT builds)
    try:
        raw = ctx.evaluate(_ORDERS_PO_COMPLETED_JS)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if result.get("ok"):
            ctx.wait_for_timeout(3000)
            log.info(
                "Orders PO Completed clicked via JS strategy=%s text=%r",
                result.get("strategy"), result.get("text", "")[:60],
            )
            return _verify_orders_list(ctx)
        log.warning("JS nav: %s", result.get("error"))
    except Exception as e:
        log.warning("JS nav exception: %s", e)

    # Fallback: try explicit text selectors
    for sel in [
        "text=/COMPLETED/i",
        'div.in_process:has-text("COMPLETED")',
        "text=COMPLETED",
    ]:
        try:
            loc = ctx.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8_000)
                ctx.wait_for_timeout(3000)
                if _verify_orders_list(ctx):
                    log.info("Navigated via selector fallback: %r", sel)
                    return True
        except Exception:
            continue

    log.warning(
        "Could not navigate to Orders > PO > Completed — "
        "run with --headful to navigate manually"
    )
    return False


def _verify_orders_list(ctx: Union[Page, Frame]) -> bool:
    """Return True when the orders list is visible."""
    checks = [
        "text=/PO NO\\./i",
        "text=Search Order Number here",
        "text=/ORDER VALUE/i",
    ]
    for _ in range(20):
        for sel in checks:
            try:
                if ctx.locator(sel).first.count() > 0:
                    return True
            except Exception:
                pass
        ctx.wait_for_timeout(500)
    return False


# ── PO extraction ─────────────────────────────────────────────────────────────

def _extract_po_entries(text: str) -> List[Dict[str, str]]:
    """Extract PO entries from DOM text or a GWT API response body.

    Returns a list of {po_number, order_id, pdf_url} dicts.
    Duplicate order_ids within the same call are collapsed.
    """
    entries: List[Dict[str, str]] = []
    seen: set[str] = set()

    for match in _PO_RE.finditer(text):
        order_id = match.group(1)
        if order_id in seen:
            continue
        seen.add(order_id)
        po_number = match.group(0)

        # Best-effort extra fields from surrounding text (may be empty)
        start = max(0, match.start() - 200)
        end = min(len(text), match.end() + 400)
        ctx_block = text[start:end]

        amount_m = _RE_AMOUNT.search(ctx_block[match.start() - start:])
        amount = amount_m.group(1) if amount_m else ""

        dates = _RE_DATE.findall(po_number)
        order_date = dates[-1] if dates else ""

        entries.append({
            "po_number": po_number,
            "order_id": order_id,
            "order_date": order_date,
            "amount": amount,
            "pdf_url": PDF_URL_TEMPLATE.format(order_id=order_id),
        })

    return entries


# ── GWT pagination helpers ────────────────────────────────────────────────────

def _parse_gwt_headers(post_data: str) -> Dict[str, str]:
    parts = post_data.split("|")
    try:
        n = int(parts[2])
        strings = parts[3: 3 + n]
        return {
            "base_url":    strings[0] if len(strings) > 0 else "",
            "permutation": strings[1] if len(strings) > 1 else "",
            "service":     strings[2] if len(strings) > 2 else "",
            "method":      strings[3] if len(strings) > 3 else "",
        }
    except (ValueError, IndexError):
        return {}


def _build_paginated_payload(
    template: str,
    page_number: int,
) -> Optional[str]:
    """Replace the LAST integer token in a GWT-RPC payload with page_number.

    Actual GWT payload format (from captured data):
      ...| 0 | 100 | PAGE_NUMBER |
            ^    ^        ^
        unknown  page_size  page index (0-based)

    The scroll-triggered payloads use page numbers 0, 1, 2, 3, ...
    We only replace the last integer field (the page index).
    """
    parts = template.rstrip("|").split("|")
    if len(parts) < 4:
        return None

    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lstrip("-").isdigit():
            parts[i] = str(page_number)
            return "|".join(parts) + "|"

    return None


# ── Scroll + DOM text extraction ──────────────────────────────────────────────

def _scroll_and_extract(
    ctx: Union[Page, Frame],
    page: Page,
    *,
    live_file: Optional[Path] = None,
    checkpoint_file: Optional[Path] = None,
    api_entries: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Scroll the orders list and extract PO entries from DOM text after each round.

    This mirrors vendor-bills/playwright_scraper.py _scroll_and_extract.
    Both DOM text AND api_entries (populated by the response listener) are
    tracked in the same deduplication set.

    Returns new entries found via DOM text (api_entries is updated in-place
    by the listener independently, so callers merge both).
    """
    all_seen: dict[str, dict] = {e["order_id"]: e for e in api_entries}
    dom_entries: dict[str, dict] = {}
    stable_rounds = 0
    live_fh = open(live_file, "w", encoding="utf-8") if live_file else None
    last_checkpoint_count = 0
    prev_api_count = len(api_entries)

    # Find the deepest scrollable container
    scroll_handle = ctx.evaluate_handle(_JS_FIND_SCROLLABLE)
    use_js_scroll = scroll_handle.as_element() is not None
    if use_js_scroll:
        log.info("Found scrollable container via JS")
    else:
        log.info("No scrollable container — using mouse wheel")

    for round_num in range(800):
        ctx.wait_for_timeout(config.SCROLL_PAUSE_MS)

        total_before = len(all_seen)

        # Read all visible text
        try:
            text = ctx.inner_text("body", timeout=15_000)
        except Exception:
            try:
                text = ctx.text_content("body", timeout=10_000) or ""
            except Exception:
                text = ""

        new_entries = _extract_po_entries(text)
        new_dom_count = 0

        for entry in new_entries:
            oid = entry["order_id"]
            if oid not in all_seen:
                all_seen[oid] = entry
                dom_entries[oid] = entry
                new_dom_count += 1
                if live_fh:
                    live_fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    live_fh.flush()

        # Sync: fold in newly API-captured entries (the listener appends in real-time)
        cur_api_count = len(api_entries)
        new_api_count = cur_api_count - prev_api_count
        prev_api_count = cur_api_count
        for e in api_entries:
            all_seen.setdefault(e["order_id"], e)

        total_after = len(all_seen)
        total_new = total_after - total_before

        # Periodic checkpoint
        if checkpoint_file and len(all_seen) - last_checkpoint_count >= 500:
            save_json(checkpoint_file, list(all_seen.values()))
            last_checkpoint_count = len(all_seen)
            log.info(
                "Checkpoint: %d entries (%d via DOM, %d via API) → %s",
                len(all_seen), len(dom_entries), len(api_entries), checkpoint_file.name,
            )

        # Idle detection: check TOTAL growth (DOM + API), not just DOM
        if total_new == 0:
            stable_rounds += 1
            if stable_rounds >= config.SCROLL_STABLE_THRESHOLD:
                log.info(
                    "No new POs for %d rounds — scroll complete (%d total, "
                    "%d DOM + %d API)",
                    stable_rounds, len(all_seen), len(dom_entries), len(api_entries),
                )
                break
        else:
            stable_rounds = 0

        if round_num % 10 == 0:
            log.info(
                "Round %d: %d total (+%d new: %d DOM, %d API) | DOM=%d API=%d",
                round_num, len(all_seen), total_new, new_dom_count, new_api_count,
                len(dom_entries), len(api_entries),
            )

        # Scroll
        if use_js_scroll:
            ctx.evaluate(_JS_SCROLL_DOWN, scroll_handle)
        else:
            try:
                ctx.evaluate(_JS_SCROLL_ALL, 3000)
            except Exception:
                page.mouse.wheel(0, 3000)

    if live_fh:
        live_fh.close()
    if checkpoint_file:
        save_json(checkpoint_file, list(all_seen.values()))

    return list(dom_entries.values())


# ── Browser-side pagination ───────────────────────────────────────────────────

def _get_captured_page_numbers(payloads_dir: Path) -> set[int]:
    """Scan all saved payloads and extract the page number (last int) from each.

    This tells us which pages the scroll phase already fetched so we skip them.
    """
    captured: set[int] = set()
    for pf in payloads_dir.glob("*.txt"):
        try:
            payload = pf.read_text(encoding="utf-8").strip()
            parts = payload.rstrip("|").split("|")
            for i in range(len(parts) - 1, -1, -1):
                if parts[i].lstrip("-").isdigit():
                    captured.add(int(parts[i]))
                    break
        except Exception:
            pass
    return captured


def _run_browser_pagination(
    page: Page,
    *,
    captured_payload: str,
    permutation: str,
    page_size: int,
    already_seen_ids: set[str],
    payloads_dir: Path,
    dumps_dir: Path,
    seq: Dict[str, int],
) -> List[Dict[str, str]]:
    """Paginate the order list from inside the live browser session.

    The GWT payload ends with ``...|0|100|PAGE_NUMBER|`` where PAGE_NUMBER is
    a 0-based page index.  We iterate page numbers 0, 1, 2, ... up to
    ceil(total_orders / page_size).  Pages already fetched during the scroll
    phase are skipped.

    Uses page.evaluate(fetch()) so the server sees the real JSESSIONID.
    """
    all_entries: List[Dict[str, str]] = []
    captured_pages = _get_captured_page_numbers(payloads_dir)

    # Upper bound: ~12000 orders / 100 per page = 120 pages, add buffer
    max_page = 200
    TINY = 300
    MAX_EMPTY = 5

    consecutive_tiny = 0
    log.info(
        "Browser pagination starting (page_size=%d, %d pages already captured) ...",
        page_size, len(captured_pages),
    )

    for page_num in range(max_page):
        if page_num in captured_pages:
            continue

        payload = _build_paginated_payload(captured_payload, page_num)
        if payload is None:
            log.warning("Could not build paginated payload for page=%d", page_num)
            break

        try:
            result = page.evaluate(_FETCH_JS, {
                "url": _ERP_URL,
                "permutation": permutation,
                "origin": config.BASE_URL,
                "payload": payload,
            })
        except Exception as e:
            log.warning("Browser pagination JS error at page=%d: %s", page_num, e)
            break

        if not isinstance(result, dict) or result.get("error"):
            log.warning(
                "Browser pagination error at page=%d: %s",
                page_num, result.get("error") if isinstance(result, dict) else result,
            )
            break

        status = result.get("status", 0)
        body = result.get("body", "") or ""

        if status != 200 or not body.strip().startswith("//OK"):
            log.info("Browser pagination stopped: status=%d at page=%d", status, page_num)
            break

        if len(body) < TINY:
            consecutive_tiny += 1
            if consecutive_tiny >= MAX_EMPTY:
                log.info("Browser pagination: no more records (page=%d)", page_num)
                break
        else:
            consecutive_tiny = 0

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        (payloads_dir / f"{stem}.txt").write_text(payload, encoding="utf-8")
        (dumps_dir / f"{stem}.txt").write_text(body, encoding="utf-8")

        page_entries = _extract_po_entries(body)
        new = [e for e in page_entries if e["order_id"] not in already_seen_ids]
        for e in new:
            already_seen_ids.add(e["order_id"])
        all_entries.extend(new)
        captured_pages.add(page_num)

        if page_num % 10 == 0 or new:
            log.info(
                "  [paginate] page=%d → %d new POs (running total: %d)",
                page_num, len(new), len(all_entries),
            )

    log.info("Browser pagination done: %d new PO(s) across %d pages", len(all_entries), len(captured_pages))
    return all_entries


# ── Main scrape function ──────────────────────────────────────────────────────

def scrape_orders(headless: bool = True) -> List[Dict[str, str]]:
    """
    Full scrape pipeline.  Returns deduplicated list of PO entry dicts.

    Saved artefacts
    ---------------
    data/po_catalog.json      — final deduped catalog
    data/po_live.jsonl        — streaming records (crash recovery)
    data/payloads/            — raw GWT POST bodies
    data/dumps/               — raw GWT responses
    """
    output_file  = config.PO_CATALOG_FILE
    live_file    = config.DATA_DIR / "po_live.jsonl"
    payloads_dir = config.DATA_DIR / "payloads"
    dumps_dir    = config.DATA_DIR / "dumps"
    for d in (payloads_dir, dumps_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Load any previously scraped data
    existing: List[Dict[str, str]] = load_json(output_file) or []
    if not existing and live_file.exists():
        existing = _recover_from_jsonl(live_file)
        if existing:
            log.info("Recovered %d entries from %s", len(existing), live_file.name)
            save_json(output_file, existing)

    existing_ids: set[str] = {e["order_id"] for e in existing}
    if existing_ids:
        log.info("Found %d existing entries — will merge new ones", len(existing_ids))

    # --- Phase 1: Playwright (scroll + API interception) ---

    # api_entries is updated in-place by the on_response listener
    api_entries: List[Dict[str, str]] = []
    api_seen_ids: set[str] = set()
    # We need these to pass captured GWT payload to pagination phase
    list_payload_template: Dict[str, str] = {"value": ""}
    list_permutation: Dict[str, str]      = {"value": ""}
    seq: Dict[str, int]                   = {"n": 0}

    def on_response(response) -> None:
        """Context-level listener: extract POs from every //OK GWT response."""
        try:
            req = response.request
            if req.method != "POST" or "/deptherp/erp" not in req.url:
                return
            pd = req.post_data
            if not pd or response.status != 200:
                return
            body = response.text()
            if not body.strip().startswith("//OK"):
                return
        except Exception:
            return

        entries = _extract_po_entries(body)
        hdrs = _parse_gwt_headers(pd)

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        (payloads_dir / f"{stem}.txt").write_text(pd, encoding="utf-8")
        (dumps_dir    / f"{stem}.txt").write_text(body, encoding="utf-8")

        # Keep the most recent payload with actual PO data as the pagination
        # template — later scroll captures are better than the initial page load.
        if entries:
            list_payload_template["value"] = pd
            perm = hdrs.get("permutation", "")
            if perm:
                list_permutation["value"] = perm
            new = [e for e in entries if e["order_id"] not in api_seen_ids
                   and e["order_id"] not in existing_ids]
            for e in new:
                api_seen_ids.add(e["order_id"])
            api_entries.extend(new)
            if seq["n"] <= 5 or len(entries) >= 50:
                log.info(
                    "  [API] %s → %d POs  (template updated, total API=%d)",
                    stem, len(entries), len(api_entries),
                )

    nav_ok = False

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=headless)
        except PlaywrightError as e:
            if "Executable doesn't exist" in str(e):
                log.error(
                    "Playwright browser missing. Run:\n"
                    "  python -m playwright install chromium"
                )
            raise

        context = browser.new_context(viewport={"width": 1600, "height": 900})
        context.on("response", on_response)
        page = context.new_page()

        try:
            _login(page)
            page.wait_for_timeout(2000)

            # Resolve the UI context (frame or page)
            ctx: Union[Page, Frame] = _get_frame_with_orders(page)

            nav_ok = _navigate_to_orders_completed(ctx)
            if nav_ok:
                ctx = _get_frame_with_orders(page)
                try:
                    ctx.locator("text=/PO NO\\./i").first.wait_for(
                        state="visible", timeout=45_000
                    )
                except Exception:
                    pass

                log.info("Scrolling orders list ...")
                dom_entries = _scroll_and_extract(
                    ctx, page,
                    live_file=live_file,
                    checkpoint_file=output_file,
                    api_entries=api_entries,
                )
                log.info(
                    "Scroll phase done: %d DOM entries, %d API entries",
                    len(dom_entries), len(api_entries),
                )

                # Phase 1.5: browser-side pagination
                if list_payload_template["value"] and list_permutation["value"]:
                    all_known_ids = (
                        existing_ids
                        | {e["order_id"] for e in dom_entries}
                        | {e["order_id"] for e in api_entries}
                    )
                    extra = _run_browser_pagination(
                        page,
                        captured_payload=list_payload_template["value"],
                        permutation=list_permutation["value"],
                        page_size=100,
                        already_seen_ids=all_known_ids,
                        payloads_dir=payloads_dir,
                        dumps_dir=dumps_dir,
                        seq=seq,
                    )
                    api_entries.extend(extra)
                    log.info("Pagination added %d new POs", len(extra))
                else:
                    log.warning(
                        "No GWT payload captured — pagination skipped. "
                        "Try --headful if navigation failed."
                    )
            else:
                log.warning("Navigation failed; no API captured for pagination.")

        except Exception as e:
            log.error("Scraper error: %s", e, exc_info=True)
            raise
        finally:
            browser.close()

    # --- Phase 2: aggregate all sources ---

    # Re-scan dump files so any pagination dumps written directly to disk
    # (not tracked in api_entries list) are included too.
    all_entries: dict[str, dict] = {e["order_id"]: e for e in existing}

    for dump_file in sorted(dumps_dir.glob("*.txt")):
        try:
            text = dump_file.read_text(encoding="utf-8")
            for e in _extract_po_entries(text):
                all_entries.setdefault(e["order_id"], e)
        except Exception:
            pass

    # Also fold in any in-memory entries (may not all be in dump files yet)
    for e in api_entries:
        all_entries.setdefault(e["order_id"], e)

    final = list(all_entries.values())
    save_json(output_file, final)
    log.info(
        "Scrape complete: %d total PO entries → %s",
        len(final), output_file,
    )
    return final


def _recover_from_jsonl(path: Path) -> List[Dict[str, str]]:
    records: dict[str, dict] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    oid = rec.get("order_id")
                    if oid:
                        records[oid] = rec
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return list(records.values())


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--headful", action="store_true")
    args = p.parse_args()

    records = scrape_orders(headless=not args.headful)
    log.info("Done: %d PO records", len(records))
