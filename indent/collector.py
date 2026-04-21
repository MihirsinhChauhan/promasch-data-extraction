"""
Playwright collector: login, navigate to Indent Completed, intercept GWT
getIndentListCompleted (/erp) and GetIndentPartsForProjectIndent (/erp2) RPCs.

Saves:
  payloads/<ts>_<n>.txt  – raw POST body
  dumps/<ts>_<n>.txt     – raw response body
  auth_state.json        – browser cookies for replay
  payload_catalog.json   – manifest with url, method, indent_id per entry

Usage:
  uv run python collector.py --user USER --password PASS [--headful] [--wait 60]
  uv run python collector.py ... --snapshot-ui [--snapshot-ui-manual] [--snapshot-ui-only]
  # Optional: indent_selectors.json in output-dir with "purchase" / "completed" keys
  # Debug: INDENT_HTTP_AUDIT=1 writes logs/http_audit.log and sample_erp_post.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from playwright.sync_api import Frame, Locator, Page, sync_playwright

from ui_probe import (
    COMPLETED_FALLBACK_SELECTORS,
    PURCHASE_NAV_SELECTORS,
    all_probe_selectors,
    get_indent_ui_context,
    gwt_soft_wait_after_action,
    load_indent_selectors_config,
    merge_completed_selectors,
    merge_purchase_selectors,
    owning_page,
    save_selector_snapshot,
    first_match,
    wait_for_post_login_shell,
)

# ── Endpoints ────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "https://gw.promasch.in"
DEFAULT_ERP_URL = "https://gw.promasch.in/deptherp/erp"
DEFAULT_ERP2_URL = "https://gw.promasch.in/deptherp/erp2"

_LIST_METHOD = "getIndentListCompleted"
_DETAIL_METHOD = "GetIndentPartsForProjectIndent"

# ── Login selector ────────────────────────────────────────────────────────────

_USER_INPUT_SEL = (
    'input[type="text"], input[type="email"], input[name*="user"], '
    'input[name*="email"], input[id*="user"], input[id*="email"], '
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"])'
    ':not([type="checkbox"])'
)

# ── GWT long encoding ─────────────────────────────────────────────────────────

# GWT-RPC v7 uses this Base64 alphabet for serialising Java longs in requests.
_GWT_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789$_"
_GWT_B64_INDEX = {c: i for i, c in enumerate(_GWT_B64)}


def encode_gwt_long(value: int) -> str:
    """Encode a non-negative Java long as GWT base-64 (used in request payloads).

    Each character encodes 6 bits, big-endian (most significant first).
    Verified: encode_gwt_long(6505) == 'Blp'.
    """
    if value == 0:
        return "A"
    chars: list[str] = []
    v = value
    while v > 0:
        chars.append(_GWT_B64[v & 0x3F])
        v >>= 6
    return "".join(reversed(chars))


def decode_gwt_long(encoded: str) -> int:
    """Decode a GWT base-64 encoded long back to an integer."""
    result = 0
    for ch in encoded:
        result = (result << 6) | _GWT_B64_INDEX[ch]
    return result


# ── Payload builders ──────────────────────────────────────────────────────────

def build_list_payload(
    base_url: str,
    permutation: str,
    offset: int = 0,
    page_size: int = 100,
) -> str:
    """Build a getIndentListCompleted GWT-RPC request payload.

    Mirrors the captured sample payload with configurable pagination offset.
    The IndentFilter is serialised with all fields zero/null except page size
    and offset (last two numeric fields in the stream).
    """
    return (
        f"7|0|6|{base_url}|{permutation}|"
        "com.l3.depth.client.proxy.ERPService|getIndentListCompleted|"
        "com.l3.depth.shared.filter.IndentFilter/1079366206|I|"
        f"1|2|3|4|3|5|6|6|5|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|"
        f"{page_size}|{offset}|"
    )


def build_detail_payload(
    base_url: str,
    permutation2: str,
    indent_id: int,
) -> str:
    """Build a GetIndentPartsForProjectIndent GWT-RPC request payload."""
    encoded = encode_gwt_long(indent_id)
    return (
        f"7|0|5|{base_url}|{permutation2}|"
        "com.l3.depth.client.proxy.ERPService2|GetIndentPartsForProjectIndent|"
        f"java.lang.Long/4227064769|1|2|3|4|1|5|5|{encoded}|"
    )


# ── Payload metadata parsers ──────────────────────────────────────────────────

def parse_indent_id_from_detail_payload(post_data: str) -> Optional[str]:
    """Return the GWT-encoded indent ID from a detail request payload."""
    stripped = post_data.rstrip("|")
    parts = stripped.split("|")
    if parts:
        last = parts[-1].strip()
        if last and not last.lstrip("-").isdigit():
            return last
        if last.isdigit():
            return last
    return None


def parse_gwt_headers_from_payload(post_data: str) -> Dict[str, str]:
    """Extract base_url, permutation, service class, method from GWT v7 payload."""
    parts = post_data.split("|")
    try:
        n = int(parts[2])
        strings = parts[3: 3 + n]
        return {
            "base_url": strings[0] if len(strings) > 0 else "",
            "permutation": strings[1] if len(strings) > 1 else "",
            "service": strings[2] if len(strings) > 2 else "",
            "method": strings[3] if len(strings) > 3 else "",
        }
    except (ValueError, IndexError):
        return {}


def detect_erp_url(post_data: str, request_url: str) -> str:
    """Determine which ERP endpoint a payload targets."""
    pl = post_data.lower()
    if (
        "/erp2" in request_url
        or "erpservice2" in pl
        or _DETAIL_METHOD.lower() in pl
    ):
        return DEFAULT_ERP2_URL
    return DEFAULT_ERP_URL


# ── Login ─────────────────────────────────────────────────────────────────────

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


# ── Navigation ────────────────────────────────────────────────────────────────
# Default Purchase / COMPLETED fallback lists live in ui_probe.PURCHASE_NAV_SELECTORS
# and ui_probe.COMPLETED_FALLBACK_SELECTORS (override via indent_selectors.json).

# JS to find the INDENTS section heading and click COMPLETED within it.
# Walks the DOM to find an element whose own text is "INDENTS", then searches
# upward through parent containers for a nearby COMPLETED link, skipping any
# that belong to the ORDERS/PO/WO sections.
_INDENT_COMPLETED_JS = """
(function() {
    function ownText(el) {
        var t = '';
        for (var i = 0; i < el.childNodes.length; i++) {
            if (el.childNodes[i].nodeType === 3) t += el.childNodes[i].textContent;
        }
        return t.trim();
    }

    function isVisible(el) {
        return !!(el && el.offsetParent !== null);
    }

    var allEls = document.querySelectorAll('*');
    var indentHeader = null;

    for (var i = 0; i < allEls.length; i++) {
        var el = allEls[i];
        var txt = ownText(el).toUpperCase();
        if (txt === 'INDENTS') {
            indentHeader = el;
            break;
        }
    }

    if (!indentHeader) return JSON.stringify({ok: false, error: 'INDENTS_NOT_FOUND'});

    // Find nearest "card-like" container that contains INDENTS heading.
    var card = indentHeader;
    for (var depth = 0; depth < 8 && card; depth++) {
        var t = card.textContent ? card.textContent.toUpperCase() : '';
        if (t.indexOf('INDENTS') >= 0 && t.indexOf('IN-PROCESS') >= 0 && t.indexOf('COMPLETED') >= 0) {
            break;
        }
        card = card.parentElement;
    }

    if (!card) return JSON.stringify({ok: false, error: 'INDENTS_CARD_NOT_FOUND'});

    // Click COMPLETED only inside INDENTS card.
    var candidates = card.querySelectorAll('a,button,div,span,td,li');
    for (var m = 0; m < candidates.length; m++) {
        var node = candidates[m];
        if (!isVisible(node)) continue;
        var txtNode = (node.textContent || '').trim();
        if (/^COMPLETED(\\s*[|:·]\\s*\\d+)?$/i.test(txtNode) || /^COMPLETED\\s*\\|/i.test(txtNode)) {
            node.click();
            return JSON.stringify({ok: true, text: txtNode, strategy: 'indent_card_completed'});
        }
    }

    // Fallback: exact text search for completed count inside card.
    var cardText = (card.textContent || '');
    if (/COMPLETED\\s*\\|\\s*\\d+/i.test(cardText)) {
        var fallback = Array.from(candidates).find(function(n) {
            return isVisible(n) && /COMPLETED\\s*\\|\\s*\\d+/i.test((n.textContent || '').trim());
        });
        if (fallback) {
            fallback.click();
            return JSON.stringify({ok: true, text: fallback.textContent.trim(), strategy: 'indent_card_completed_count'});
        }
    }

    // Last resort: nearest visible completed element around INDENTS header.
    var root = indentHeader.parentElement || document.body;
    for (var climb = 0; climb < 4 && root; climb++) {
        var nearby = root.querySelectorAll('a,button,div,span,td,li');
        for (var n = 0; n < nearby.length; n++) {
            var link = nearby[n];
            var txt2 = (link.textContent || '').trim();
            if (isVisible(link) && /^COMPLETED/i.test(txt2)) {
                link.click();
                return JSON.stringify({ok: true, text: txt2, strategy: 'nearest_completed'});
            }
        }
        root = root.parentElement;
    }
    return JSON.stringify({ok: false, error: 'COMPLETED_NOT_FOUND_IN_INDENTS'});
})()
"""


def navigate_to_indent_completed(
    ctx: Union[Page, Frame],
    purchase_selectors: List[str],
    completed_fallback_selectors: List[str],
) -> bool:
    """Navigate to Indent Completed via the sidebar Purchase section.

    Step 1 – click "Purchase | Work Order | Challans | Vendor Bills" in the
              left sidebar (this opens the RFQs/VCs/Indents/Orders overview).
    Step 2 – click "COMPLETED" inside the INDENTS card on that overview page
              using a JS DOM walk that anchors on the INDENTS heading.
    """
    # ── Step 1: sidebar navigation ───────────────────────────────────────────
    loc, sel_used = first_match(ctx, purchase_selectors)
    if loc is None:
        print(
            "[collector] WARNING: Could not click sidebar 'Purchase' section. "
            "If running --headful, please navigate manually."
        )
        return False

    try:
        loc.first.click(timeout=10_000)
    except Exception as e:
        print(f"[collector] Purchase click failed ({sel_used!r}): {e}")
        return False

    gwt_soft_wait_after_action(ctx)
    print(f"[collector] Sidebar clicked via: {sel_used!r}")

    # ── Step 2: click COMPLETED under INDENTS ────────────────────────────────
    # Strategy A: JavaScript DOM walk anchored on INDENTS heading
    try:
        raw = ctx.evaluate(_INDENT_COMPLETED_JS)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if result.get("ok"):
            gwt_soft_wait_after_action(ctx)
            print(
                f"[collector] COMPLETED clicked via JS: {result.get('text', '')} "
                f"(strategy={result.get('strategy', '')})"
            )
            if _ensure_indent_completed_list(ctx):
                return True
        print(f"[collector] JS navigation: {result.get('error', 'unknown')}")
    except Exception as e:
        print(f"[collector] JS navigation failed: {e}")

    # Strategy B: explicitly click INDENTS → COMPLETED in the left section card.
    for sel in completed_fallback_selectors:
        try:
            cloc = ctx.locator(sel).first
            if cloc.count() > 0:
                cloc.click(timeout=10_000)
                gwt_soft_wait_after_action(ctx)
                print(f"[collector] COMPLETED clicked via fallback: {sel!r}")
                if _ensure_indent_completed_list(ctx):
                    return True
        except Exception:
            continue

    # Strategy C: If section click worked but list did not switch, force filters.
    if _force_indent_completed_filters(ctx):
        return True

    print(
        "[collector] WARNING: Could not click COMPLETED under INDENTS. "
        "If running --headful, please click it manually."
    )
    return False


# ── Scroll & click helpers for indent list ────────────────────────────────────

def _ensure_indent_completed_list(ctx: Union[Page, Frame]) -> bool:
    """Verify list view has Indents + Completed filters selected."""
    checks = [
        "text=/INDENT NO\\./i",
        "text=Search Indent Number here",
        "text=Completed",
    ]
    for _ in range(10):
        for sel in checks:
            try:
                if ctx.locator(sel).first.count() > 0:
                    return True
            except Exception:
                continue
        ctx.wait_for_timeout(500)
    return False


def _force_indent_completed_filters(ctx: Union[Page, Frame]) -> bool:
    """Set top filter dropdowns to Indents + Completed (UI-only fallback)."""
    try:
        for sel in [
            "select:near(:text('Search Indent Number here')) >> nth=0",
            "select >> nth=0",
        ]:
            dd = ctx.locator(sel).first
            if dd.count() == 0:
                continue
            try:
                dd.select_option(label="Indents", timeout=3000)
                break
            except Exception:
                try:
                    dd.select_option(value="Indents", timeout=3000)
                    break
                except Exception:
                    continue

        for sel in [
            "select:near(:text('Search Indent Number here')) >> nth=1",
            "select >> nth=1",
        ]:
            dd = ctx.locator(sel).first
            if dd.count() == 0:
                continue
            try:
                dd.select_option(label="Completed", timeout=3000)
                break
            except Exception:
                try:
                    dd.select_option(value="Completed", timeout=3000)
                    break
                except Exception:
                    continue

        gwt_soft_wait_after_action(ctx, settle_ms=2000)
    except Exception:
        return False
    return _ensure_indent_completed_list(ctx)

def _build_list_scroll_js(delta_y: int = 800) -> str:
    """Return JS that scrolls the widest scrollable container by delta_y px."""
    return f"""
    (function() {{
        var divs = Array.from(document.querySelectorAll('div'));
        var best = null, bestScore = 0;
        divs.forEach(function(d) {{
            if (d.scrollHeight <= d.clientHeight + 50) return;
            var rect = d.getBoundingClientRect();
            if (rect.width < 300 || rect.left < 150) return;
            var score = d.scrollHeight - d.clientHeight;
            if (score > bestScore) {{ best = d; bestScore = score; }}
        }});
        if (best) {{
            best.scrollBy(0, {delta_y});
            return best.className ? best.className.slice(0, 60) : 'unnamed';
        }}
        window.scrollBy(0, {delta_y});
        return 'window_fallback';
    }})()
    """


def _scroll_indent_list(
    ctx: Union[Page, Frame],
    seq: Dict[str, int],
    *,
    max_rounds: int = 30,
    idle_threshold: int = 5,
    scroll_delta: int = 800,
) -> int:
    """Scroll the indent list to trigger lazy-loading of more rows.

    Returns the number of new RPC calls triggered by scrolling.
    """
    scroll_js = _build_list_scroll_js(scroll_delta)
    scroll_all_js = f"""
    (function() {{
        var divs = Array.from(document.querySelectorAll('div'));
        var count = 0;
        divs.forEach(function(d) {{
            if (d.scrollHeight > d.clientHeight + 50) {{
                var rect = d.getBoundingClientRect();
                if (rect.width >= 250 && rect.height >= 120 && rect.left >= 120) {{
                    d.scrollBy(0, {scroll_delta});
                    count++;
                }}
            }}
        }});
        if (count === 0) window.scrollBy(0, {scroll_delta});
        return count;
    }})()
    """
    prev_n = seq["n"]
    idle_count = 0
    last_rpc_n = seq["n"]
    pw = owning_page(ctx)

    for round_num in range(max_rounds):
        try:
            panel = ctx.evaluate(scroll_js)
            if round_num == 0:
                print(f"[collector] Scrolling list panel: {panel}")
            if round_num % 3 == 2:
                scrolled = ctx.evaluate(scroll_all_js)
                if scrolled:
                    print(f"[collector] Broad-scroll pass touched {scrolled} container(s)")
        except Exception:
            pw.mouse.move(800, 450)
            pw.mouse.wheel(0, scroll_delta)

        ctx.wait_for_timeout(1500)

        if seq["n"] == last_rpc_n:
            idle_count += 1
            if idle_count >= idle_threshold:
                break
        else:
            idle_count = 0
            last_rpc_n = seq["n"]

    new_rpcs = seq["n"] - prev_n
    print(f"[collector] Scrolling done: {new_rpcs} new RPC(s) captured")
    return new_rpcs


_CLOSE_POPUP_JS = """
(function() {
    var sels = [
        '[class*="popupClose"]', '[class*="PopupClose"]',
        '[class*="closeButton"]', '[class*="close-button"]',
        '[class*="dialogClose"]', '.gwt-DialogBox .close',
        'button[class*="close"]', 'div[class*="close"]', 'a[class*="close"]'
    ];
    for (var i = 0; i < sels.length; i++) {
        var el = document.querySelector(sels[i]);
        if (el && el.offsetParent !== null) { el.click(); return 'closed:' + sels[i]; }
    }
    var all = document.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        var el = all[j];
        var rect = el.getBoundingClientRect();
        var text = el.textContent.trim();
        if ((text === '\\u00d7' || text === 'X' || text === '\\u2715' || text === '\\u2716')
            && rect.width < 50 && rect.height < 50 && el.offsetParent !== null) {
            el.click(); return 'closed:x_char';
        }
    }
    return 'not_found';
})()
"""


def _close_detail_popup(ctx: Union[Page, Frame]) -> bool:
    """Close the indent detail popup using JS, Escape, or click-away."""
    pw = owning_page(ctx)
    for attempt in [
        lambda: _try_js_close(ctx),
        lambda: pw.keyboard.press("Escape"),
        lambda: pw.mouse.click(5, 5),
    ]:
        try:
            attempt()
            ctx.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


def _try_js_close(ctx: Union[Page, Frame]) -> None:
    result = ctx.evaluate(_CLOSE_POPUP_JS)
    if not (isinstance(result, str) and result.startswith("closed")):
        raise RuntimeError(result)


def _click_indent_rows(
    ctx: Union[Page, Frame],
    seq: Dict[str, int],
    *,
    max_clicks: int = 5,
    detail_wait_seconds: int = 15,
) -> int:
    """Click on indent rows to trigger GetIndentPartsForProjectIndent API calls.

    Returns the number of detail responses captured.
    """
    # :has-text() only accepts a plain substring in CSS; regex belongs in .filter(has_text=re.compile(...)).
    card_candidates: List[Union[str, Callable[[Union[Page, Frame]], Locator]]] = [
        "div:has-text('INDENT NO.')",
        "div:has-text('Indent No.')",
        "div:has-text('INDENT STATUS')",
        lambda c: c.locator("div").filter(has_text=re.compile(r"INDENT\s*NO\.?", re.I)),
        lambda c: c.locator("tr").filter(has_text=re.compile(r"Ind-\d+", re.I)),
        lambda c: c.locator("table >> tr").filter(
            has_text=re.compile(r"J-[A-Z0-9-]+-Ind-", re.I)
        ),
    ]
    row_cards: Optional[Locator] = None
    count = 0
    for item in card_candidates:
        loc = item(ctx) if callable(item) else ctx.locator(item)
        c = loc.count()
        if c > count:
            row_cards = loc
            count = c

    if count == 0:
        print("[collector] No indent rows found to click")
        return 0

    print(f"[collector] Found {count} indent rows, clicking up to {max_clicks}")
    detail_captured = 0

    for i in range(min(count, max_clicks)):
        prev_n = seq.get("detail_n", seq["n"])
        try:
            card = row_cards.nth(i)
            card.scroll_into_view_if_needed(timeout=5000)
            clicked = False
            for sub_sel in [
                "text=/J-[A-Z0-9-]+-Ind-\\d+(?:[/-]\\d+)?/",
                "text=/\\bInd-\\d+\\b/",
            ]:
                sub = card.locator(sub_sel).first
                if sub.count() > 0:
                    sub.click(timeout=5000)
                    clicked = True
                    break
            if not clicked:
                card.click(timeout=5000)
        except Exception as e:
            print(f"  [click] indent {i + 1}: click failed: {e}")
            continue

        captured = False
        for _ in range(detail_wait_seconds * 2):
            ctx.wait_for_timeout(500)
            if seq.get("detail_n", seq["n"]) > prev_n:
                captured = True
                break
            try:
                if ctx.locator("text=/Indent\\s*\\(Completed\\)/i").first.count() > 0:
                    captured = True
                    break
            except Exception:
                pass

        if captured:
            detail_captured += 1
            print(f"  [click] indent {i + 1}: detail response captured")
        else:
            print(f"  [click] indent {i + 1}: no detail response (timeout)")

        _close_detail_popup(ctx)
        ctx.wait_for_timeout(1000)

    print(
        f"[collector] Clicked {min(count, max_clicks)} indents, "
        f"captured {detail_captured} detail(s)"
    )
    return detail_captured


# ── Collection ────────────────────────────────────────────────────────────────

def run_collection(
    output_dir: Path,
    base_url: str,
    user: str,
    password: str,
    headful: bool,
    wait_seconds: int = 60,
    auto_paginate: bool = True,
    page_size: int = 100,
    max_detail_clicks: int = 5,
    *,
    snapshot_ui: bool = False,
    snapshot_ui_manual: bool = False,
    snapshot_ui_only: bool = False,
) -> List[Dict[str, Any]]:
    """
    Phase 1 – Playwright: login, navigate, scroll list, click indents for details.
    Phase 2 – HTTP replay: paginate list API for remaining pages (if auto_paginate).

    Returns the combined payload catalog.
    """
    snap_login = snapshot_ui or snapshot_ui_only

    output_dir.mkdir(parents=True, exist_ok=True)
    payloads_dir = output_dir / "payloads"
    dumps_dir = output_dir / "dumps"
    logs_dir = output_dir / "logs"
    for d in (payloads_dir, dumps_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    catalog: List[Dict[str, Any]] = []
    seq = {"n": 0, "detail_n": 0, "list_n": 0}
    captured_headers: Dict[str, Dict[str, str]] = {}
    captured_permutation2: Dict[str, str] = {"value": ""}
    _audit_erp_post_saved = False
    _http_audit = os.environ.get("INDENT_HTTP_AUDIT", "").strip() in (
        "1",
        "true",
        "yes",
    )

    def on_response(response) -> None:
        nonlocal _audit_erp_post_saved
        try:
            req = response.request
            if req.method != "POST":
                return
            url = req.url
            if "deptherp" not in url:
                return
            pd = req.post_data
            if _http_audit and pd and "/deptherp/erp" in url:
                with (logs_dir / "http_audit.log").open("a", encoding="utf-8") as af:
                    pl = (pd or "").lower()
                    af.write(
                        f"{time.time()}: status={response.status} "
                        f"pd_len={len(pd or '')} "
                        f"list={_LIST_METHOD.lower() in pl} "
                        f"detail={_DETAIL_METHOD.lower() in pl} "
                        f"{url[:160]}\n"
                    )
                if (
                    "/deptherp/erp" in url
                    and "remote_logging" not in url
                    and not _audit_erp_post_saved
                ):
                    (logs_dir / "sample_erp_post.txt").write_text(
                        (pd or "")[:8000], encoding="utf-8"
                    )
                    _audit_erp_post_saved = True
            if "/deptherp/erp" not in url:
                return
            if not pd:
                return
            pl = pd.lower()
            list_hit = _LIST_METHOD.lower() in pl
            detail_hit = _DETAIL_METHOD.lower() in pl
            if not list_hit and not detail_hit:
                return
            if response.status != 200:
                return
            body = response.text()
        except Exception as e:
            with (logs_dir / "collector_errors.log").open("a", encoding="utf-8") as lf:
                lf.write(f"{time.time()}: {e}\n")
            return

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        payload_path = payloads_dir / f"{stem}.txt"
        dump_path = dumps_dir / f"{stem}.txt"
        payload_path.write_text(pd, encoding="utf-8")
        dump_path.write_text(body, encoding="utf-8")

        pl = pd.lower()
        is_detail = _DETAIL_METHOD.lower() in pl
        method_name = _DETAIL_METHOD if is_detail else _LIST_METHOD
        indent_id = parse_indent_id_from_detail_payload(pd) if is_detail else None
        erp_url = detect_erp_url(pd, url)

        hdrs = parse_gwt_headers_from_payload(pd)
        if hdrs:
            captured_headers[method_name] = hdrs
            if is_detail and hdrs.get("permutation"):
                captured_permutation2["value"] = hdrs["permutation"]

        entry: Dict[str, Any] = {
            "dump": str(dump_path.relative_to(output_dir)),
            "payload": str(payload_path.relative_to(output_dir)),
            "captured_at_ms": int(time.time() * 1000),
            "url": erp_url,
            "method": method_name,
            "indent_id": indent_id,
        }
        catalog.append(entry)
        if is_detail:
            seq["detail_n"] = seq.get("detail_n", 0) + 1
        else:
            seq["list_n"] = seq.get("list_n", 0) + 1
        tag = f" (indent={indent_id})" if indent_id else ""
        print(f"  [captured] {method_name} → {stem}.txt{tag}")

    # ── Phase 1: Playwright capture ───────────────────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        # GWT XHR often originates from iframes; Page-level listeners miss those.
        context.on("response", on_response)
        page = context.new_page()

        login_and_wait(page, base_url, user, password)

        shell = wait_for_post_login_shell(page)
        if shell is not None:
            print("[collector] Post-login shell ready.")
            ctx: Union[Page, Frame] = shell
        else:
            print(
                "[collector] WARNING: Post-login shell not detected within timeout; "
                "snapshots and navigation may still see login DOM only."
            )
            ctx = get_indent_ui_context(page)
        page.wait_for_timeout(1500)

        sel_cfg = load_indent_selectors_config(output_dir)
        purchase_selectors = merge_purchase_selectors(PURCHASE_NAV_SELECTORS, sel_cfg)
        completed_selectors = merge_completed_selectors(
            COMPLETED_FALLBACK_SELECTORS, sel_cfg
        )

        if snap_login:
            save_selector_snapshot(ctx, output_dir, "login", all_probe_selectors())
            print(
                f"[collector] UI snapshot (login) → "
                f"{output_dir}/selector_probe_login.json"
            )

        if snapshot_ui_manual:
            print(
                f"[collector] Manual navigation window: {wait_seconds}s, "
                "then post_nav snapshot..."
            )
            page.wait_for_timeout(wait_seconds * 1000)
            ctx = get_indent_ui_context(page)
            save_selector_snapshot(ctx, output_dir, "post_nav", all_probe_selectors())
            print(
                f"[collector] UI snapshot (post_nav) → "
                f"{output_dir}/selector_probe_post_nav.json"
            )

        if snapshot_ui_only:
            nav_ok = False
        else:
            nav_ok = navigate_to_indent_completed(
                ctx, purchase_selectors, completed_selectors
            )

        if nav_ok:
            ctx = get_indent_ui_context(page)
            try:
                ctx.locator("text=/INDENT NO\\./i").first.wait_for(
                    state="visible", timeout=45_000
                )
            except Exception:
                pass
            print("[collector] Scrolling indent list for lazy loading...")
            _scroll_indent_list(ctx, seq, max_rounds=30, idle_threshold=5)

            print("[collector] Clicking indent rows for detail capture...")
            _click_indent_rows(
                ctx, seq, max_clicks=max_detail_clicks, detail_wait_seconds=15,
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

    if captured_permutation2["value"]:
        perm_path = output_dir / "permutation2.txt"
        perm_path.write_text(captured_permutation2["value"], encoding="utf-8")
        print(f"[collector] Permutation2 saved → {perm_path}")

    # ── Phase 2: Paginate list API via direct HTTP ────────────────────────────
    if auto_paginate and _LIST_METHOD in captured_headers:
        _run_list_pagination(
            catalog=catalog,
            captured_headers=captured_headers,
            payloads_dir=payloads_dir,
            dumps_dir=dumps_dir,
            logs_dir=logs_dir,
            auth_path=output_dir / "auth_state.json",
            output_dir=output_dir,
            page_size=page_size,
            seq=seq,
        )

    # ── Save catalog ──────────────────────────────────────────────────────────
    catalog_path = output_dir / "payload_catalog.json"
    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[collector] Catalog: {len(catalog)} entries → {catalog_path}")
    return catalog


def _run_list_pagination(
    *,
    catalog: List[Dict[str, Any]],
    captured_headers: Dict[str, Dict[str, str]],
    payloads_dir: Path,
    dumps_dir: Path,
    logs_dir: Path,
    auth_path: Path,
    output_dir: Path,
    page_size: int,
    seq: Dict[str, int],
) -> None:
    """Replay list API with successive offsets until fewer than page_size results."""
    import requests  # imported here to avoid requiring it for Playwright-only runs
    from urllib.parse import urlparse

    hdrs = captured_headers[_LIST_METHOD]
    base_url = hdrs.get("base_url", "https://gw.promasch.in/deptherp/")
    permutation = hdrs.get("permutation", "")

    if not permutation:
        print("[collector] Cannot paginate: no permutation captured.")
        return

    # Load cookies
    if not auth_path.is_file():
        print("[collector] Cannot paginate: auth_state.json missing.")
        return
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

    # Find which offsets were already captured
    captured_offsets: set[int] = set()
    for entry in catalog:
        if entry.get("method") == _LIST_METHOD:
            try:
                pd = (output_dir / entry["payload"]).read_text(encoding="utf-8")
                parts = pd.rstrip("|").split("|")
                offset = int(parts[-1])
                captured_offsets.add(offset)
            except Exception:
                pass
    captured_offsets.add(0)  # assume first page already captured

    offset = page_size  # start from page 2
    consecutive_small = 0

    while True:
        if offset in captured_offsets:
            offset += page_size
            continue

        payload = build_list_payload(base_url, permutation, offset, page_size)
        try:
            resp = requests.post(
                DEFAULT_ERP_URL,
                data=payload.encode("utf-8"),
                headers=http_headers,
                cookies=cookies,
                timeout=120.0,
            )
        except Exception as e:
            print(f"[collector] Pagination request failed at offset={offset}: {e}")
            break

        if resp.status_code != 200 or not resp.text.strip().startswith("//OK"):
            print(
                f"[collector] Pagination stopped: status={resp.status_code} "
                f"at offset={offset}"
            )
            break

        seq["n"] += 1
        stem = f"{int(time.time() * 1000)}_{seq['n']}"
        payload_path = payloads_dir / f"{stem}.txt"
        dump_path = dumps_dir / f"{stem}.txt"
        payload_path.write_text(payload, encoding="utf-8")
        dump_path.write_text(resp.text, encoding="utf-8")

        catalog.append({
            "dump": str(dump_path.relative_to(output_dir)),
            "payload": str(payload_path.relative_to(output_dir)),
            "captured_at_ms": int(time.time() * 1000),
            "url": DEFAULT_ERP_URL,
            "method": _LIST_METHOD,
            "indent_id": None,
            "offset": offset,
        })
        print(f"  [paginate] {_LIST_METHOD} offset={offset} → {stem}.txt")

        try:
            from gwt_parser import extract_indent_ids_from_list_response
            page_ids = extract_indent_ids_from_list_response(resp.text)
            if len(page_ids) == 0:
                consecutive_small += 1
                if consecutive_small >= 2:
                    print("[collector] Pagination: no indent IDs in response, stopping.")
                    break
            else:
                consecutive_small = 0
        except Exception:
            body_len = len(resp.text)
            if body_len < 5000:
                consecutive_small += 1
                if consecutive_small >= 2:
                    print("[collector] Pagination: empty-looking pages, stopping.")
                    break
            else:
                consecutive_small = 0

        offset += page_size


# ── Build detail payloads from captured list responses ─────────────────────────

def build_detail_payloads_from_catalog(
    output_dir: Path,
    permutation2: str,
) -> int:
    """
    Parse all captured list responses, extract indent IDs, and write detail
    request payloads to payloads/ (adding them to payload_catalog.json).

    Requires knowing the ERPService2 permutation (from a captured detail payload
    or passed explicitly via CLI).
    """
    catalog_path = output_dir / "payload_catalog.json"
    if not catalog_path.is_file():
        print("[collector] payload_catalog.json not found; run --collect first.")
        return 0

    catalog: List[Dict[str, Any]] = json.loads(
        catalog_path.read_text(encoding="utf-8")
    )

    # Find base_url from list entries
    base_url = "https://gw.promasch.in/deptherp/"
    for entry in catalog:
        if entry.get("method") == _LIST_METHOD:
            try:
                pd = (output_dir / entry["payload"]).read_text(encoding="utf-8")
                hdrs = parse_gwt_headers_from_payload(pd)
                if hdrs.get("base_url"):
                    base_url = hdrs["base_url"]
                    break
            except Exception:
                pass

    # Extract indent IDs from list dump files
    from gwt_parser import extract_indent_ids_from_list_response

    indent_ids: set[int] = set()
    for entry in catalog:
        if entry.get("method") != _LIST_METHOD:
            continue
        try:
            dump = (output_dir / entry["dump"]).read_text(encoding="utf-8")
            ids = extract_indent_ids_from_list_response(dump)
            indent_ids.update(ids)
        except Exception as e:
            print(f"[collector] Warning: could not parse list dump: {e}")

    print(f"[collector] Found {len(indent_ids)} indent IDs.")

    # Check which IDs are already captured
    captured_ids: set[str] = {
        e["indent_id"]
        for e in catalog
        if e.get("method") == _DETAIL_METHOD and e.get("indent_id")
    }

    payloads_dir = output_dir / "payloads"
    dumps_dir = output_dir / "dumps"
    payloads_dir.mkdir(exist_ok=True)
    dumps_dir.mkdir(exist_ok=True)

    added = 0
    for indent_id in sorted(indent_ids):
        encoded = encode_gwt_long(indent_id)
        if encoded in captured_ids:
            continue

        stem = f"detail_{indent_id}"
        payload_path = payloads_dir / f"{stem}.txt"
        dump_path = dumps_dir / f"{stem}.txt"
        payload = build_detail_payload(base_url, permutation2, indent_id)
        payload_path.write_text(payload, encoding="utf-8")

        catalog.append({
            "dump": str(dump_path.relative_to(output_dir)),
            "payload": str(payload_path.relative_to(output_dir)),
            "captured_at_ms": int(time.time() * 1000),
            "url": DEFAULT_ERP2_URL,
            "method": _DETAIL_METHOD,
            "indent_id": encoded,
            "source": "built_from_list",
        })
        added += 1

    catalog_path.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"[collector] Added {added} detail payload(s) to catalog.")
    return added


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture Promasch GWT indent-completed payloads"
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument(
        "--user",
        default=os.getenv("INDENT_USER", os.getenv("CONSTRUCTIONARY_USER", "")),
    )
    p.add_argument(
        "--password",
        default=os.getenv("INDENT_PASSWORD", os.getenv("CONSTRUCTIONARY_PASSWORD", "")),
    )
    p.add_argument("--headful", action="store_true")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Data directory (default: api-extraction/indent/data/<run_id>)",
    )
    p.add_argument(
        "--wait",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Seconds to wait for API calls after navigation (default: 60)",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Indent list page size (default: 100)",
    )
    p.add_argument(
        "--no-paginate",
        action="store_true",
        help="Skip automatic list pagination after Playwright phase",
    )
    p.add_argument(
        "--build-detail-payloads",
        action="store_true",
        help="Build detail request payloads from captured list responses",
    )
    p.add_argument(
        "--permutation2",
        default="",
        metavar="HASH",
        help="ERPService2 permutation hash for building detail payloads",
    )
    p.add_argument(
        "--snapshot-ui",
        action="store_true",
        help="After login, save snapshot_login.html + css_classes + selector_probe",
    )
    p.add_argument(
        "--snapshot-ui-manual",
        action="store_true",
        help="After login snapshot, wait --wait seconds then save post_nav probe",
    )
    p.add_argument(
        "--snapshot-ui-only",
        action="store_true",
        help="Snapshot(s) then skip automated navigation (still saves auth_state.json)",
    )
    return p.parse_args()


def ensure_credentials(user: str, password: str) -> None:
    if not user or not password:
        raise SystemExit(
            "Missing credentials. Pass --user/--password or set "
            "INDENT_USER / INDENT_PASSWORD environment variables."
        )


def main() -> None:
    args = parse_args()
    ensure_credentials(args.user, args.password)

    root = Path(__file__).resolve().parent
    out = args.output_dir or (root / "data" / str(int(time.time())))
    out = out.resolve()

    if args.build_detail_payloads:
        if not args.permutation2:
            raise SystemExit(
                "--permutation2 is required for --build-detail-payloads. "
                "Find it in a captured detail payload (2nd pipe segment after the base URL)."
            )
        build_detail_payloads_from_catalog(out, args.permutation2)
        return

    run_collection(
        output_dir=out,
        base_url=args.base_url,
        user=args.user,
        password=args.password,
        headful=args.headful,
        wait_seconds=args.wait,
        auto_paginate=not args.no_paginate,
        page_size=args.page_size,
        snapshot_ui=args.snapshot_ui,
        snapshot_ui_manual=args.snapshot_ui_manual,
        snapshot_ui_only=args.snapshot_ui_only,
    )


if __name__ == "__main__":
    main()
