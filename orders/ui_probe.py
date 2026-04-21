"""
Orders UI probing: selector definitions and snapshot utilities.

Provides frame detection, selector snapshots (HTML + css_classes + probe counts),
first_match over candidate lists, and navigation selectors for Orders > PO > Completed.

Usage (standalone probe):
  uv run python ui_probe.py               # dry-run: print selectors
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from playwright.sync_api import Frame, Locator, Page

# ── Frame markers (try main page then iframes) ────────────────────────────────

_ORDERS_UI_MARKER_LOCS = [
    "text=/ORDERS/i",
    "text=/Purchase/i",
    "text=/Search Order Number/i",
    "text=/PO NO\\./i",
]

# ── Purchase sidebar selectors (shared with indent) ──────────────────────────

PURCHASE_NAV_SELECTORS: List[str] = [
    'td[role="menuitem"][title="Purchase"]',
    "td.gwt-MenuItem[title='Purchase']",
    'td.gwt-MenuItem:has-text("Purchase | Work Order | Challans | Vendor Bills")',
    'div#menuText:has-text("Purchase | Work Order")',
    "text=Purchase | Work Order | Challans | Vendor Bills",
    "a:has-text('Purchase | Work Order')",
    "a:has-text('Purchase')",
    "div:has-text('Purchase | Work Order | Challans | Vendor Bills')",
    "span:has-text('Purchase | Work Order | Challans | Vendor Bills')",
]

# ── Orders > PO > Completed sidebar selectors ────────────────────────────────
# DOM structure (from snapshot_post_purchase.html):
#   <div class="INDENTS">ORDERS</div>       ← ORDERS heading (shares class with INDENTS!)
#     <div class="POName">PO</div>           ← PO sub-section heading
#       <div class="in_process max_content">RAISED | 16</div>
#       <div class="in_process max_content">IN-PROCESS | 103</div>
#       <div class="in_process max_content">COMPLETED | 11733</div>  ← CLICK THIS
#     <div class="POName" style="font-size:10px">WO</div>            ← WO heading
#       <div class="in_process max_content">COMPLETED | ...</div>    ← DO NOT CLICK
#
# NOTE: Only valid AFTER clicking the Purchase top-menu tab.
# On the HOME DASHBOARD, these selectors are false positives.

ORDERS_COMPLETED_SELECTORS: List[str] = [
    # Best: use CSS classes found in snapshot — PO section COMPLETED (comes before WO)
    "div.POName:text-is('PO') ~ table div.in_process:has-text('COMPLETED')",
    "table:has(div.POName:text-is('PO')) div.in_process:has-text('COMPLETED')",
    # Fallback with count pattern
    "div.in_process:has-text('COMPLETED | 11')",
    "div.in_process:has-text('COMPLETED | 1')",
    "div.in_process:text-matches('COMPLETED \\\\| \\\\d+')",
    # Generic fallbacks
    "div:has-text('ORDERS') >> div:has-text('PO') >> div.in_process:has-text('COMPLETED')",
    "div:has-text('ORDERS') >> text=/COMPLETED\\s*\\|\\s*\\d+/",
    "div:has-text('ORDERS') >> text=COMPLETED",
]

# ── Post-purchase page probe selectors ───────────────────────────────────────
# Selectors observed in snapshot_post_purchase.html

POST_PURCHASE_PROBE_SELECTORS: List[str] = [
    # Sidebar CSS class selectors (from actual DOM)
    "div.INDENTS",                                     # ORDERS heading (shares class!)
    "div.POName",                                      # PO + WO headings
    "div.POName:text-is('PO')",                        # PO heading only
    "div.POName:text-is('WO')",                        # WO heading only
    "div.in_process",                                  # all RAISED/IN-PROCESS/COMPLETED
    "div.in_process:has-text('COMPLETED')",            # all COMPLETED items
    "div.in_process:has-text('COMPLETED | 11')",       # PO COMPLETED (count starts 11)
    # Text-based fallbacks
    "text=VENDOR COMPARISONS",
    "text=INDENTS",
    "text=ORDERS",
    "text=Vendor Bills",
    "text=PO",
    "text=WO",
    "text=/IN-PROCESS/i",
    "text=/RAISED/i",
    "div:has-text('ORDERS') >> text=/COMPLETED\\s*\\|\\s*\\d+/",
    # Dropdown (may be absent)
    "select >> nth=0",
]

# ── List presence verification selectors ─────────────────────────────────────

LIST_PROBE_SELECTORS: List[str] = [
    "text=/PO NO\\./i",
    "text=Search Order Number here",
    "text=Completed",
    "div:has-text('PO NO.')",
    "text=/ORDER VALUE STATUS/i",
    "text=/VENDOR/i",
    "text=/TOTAL VALUE/i",
]

SELECTORS_CONFIG_NAME = "order_selectors.json"


def all_probe_selectors() -> List[str]:
    """Single deduped list of all candidate selectors for snapshot probing."""
    seen: set[str] = set()
    out: List[str] = []
    for s in (
        PURCHASE_NAV_SELECTORS
        + ORDERS_COMPLETED_SELECTORS
        + POST_PURCHASE_PROBE_SELECTORS
        + LIST_PROBE_SELECTORS
    ):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def post_purchase_probe_selectors() -> List[str]:
    """Selectors specifically for probing the post-Purchase-click page."""
    seen: set[str] = set()
    out: List[str] = []
    for s in (
        POST_PURCHASE_PROBE_SELECTORS
        + ORDERS_COMPLETED_SELECTORS
        + LIST_PROBE_SELECTORS
    ):
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ── Shared playwright helpers ─────────────────────────────────────────────────

def owning_page(ctx: Union[Page, Frame]) -> Page:
    """Page to use for mouse/keyboard when ctx may be a Frame."""
    return ctx.page if isinstance(ctx, Frame) else ctx


def first_match(
    ctx: Union[Page, Frame],
    selectors: List[str],
) -> Tuple[Optional[Locator], Optional[str]]:
    """Return (locator, selector_string) for first selector with count > 0."""
    for sel in selectors:
        try:
            loc = ctx.locator(sel)
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, None


def get_orders_ui_context(page: Page) -> Union[Page, Frame]:
    """Return the frame (or main page) containing Orders / Purchase UI markers."""
    for sel in _ORDERS_UI_MARKER_LOCS:
        try:
            if page.locator(sel).count() > 0:
                return page
        except Exception:
            continue

    for frame in page.frames:
        if not frame.url:
            continue
        try:
            for sel in _ORDERS_UI_MARKER_LOCS:
                if frame.locator(sel).count() > 0:
                    return frame
        except Exception:
            continue

    return page


_POST_LOGIN_SHELL_SELECTORS: List[str] = [
    "td.gwt-MenuItem",
    "table.gwt-MenuBar",
    "[class*='gwt-MenuBar']",
    "div#bodyDiv td.gwt-MenuItem",
    "#menuText",
]


def wait_for_post_login_shell(
    page: Page, timeout_ms: int = 120_000
) -> Optional[Union[Page, Frame]]:
    """Poll main page and frames until GWT menu chrome appears."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for ctx in [page, *page.frames]:
            try:
                for sel in _POST_LOGIN_SHELL_SELECTORS:
                    loc = ctx.locator(sel)
                    if loc.count() > 0:
                        try:
                            loc.first.wait_for(state="visible", timeout=5_000)
                        except Exception:
                            pass
                        return ctx
            except Exception:
                continue
        page.wait_for_timeout(400)
    return None


# ── Snapshot utilities ────────────────────────────────────────────────────────

def _collect_css_classes(ctx: Union[Page, Frame]) -> List[str]:
    return ctx.evaluate(
        """() => {
        const all = document.querySelectorAll('[class]');
        const seen = new Set();
        all.forEach(el => {
            el.className.split(' ').forEach(c => { if (c) seen.add(c); });
        });
        return [...seen].sort();
    }"""
    )


def save_selector_snapshot(
    ctx: Union[Page, Frame],
    run_dir: Path,
    name: str,
    candidate_selectors: List[str],
) -> None:
    """Write snapshot_{name}.html, css_classes_{name}.json, selector_probe_{name}.json."""
    run_dir.mkdir(parents=True, exist_ok=True)
    html = ctx.content()
    (run_dir / f"snapshot_{name}.html").write_text(html, encoding="utf-8")

    classes = _collect_css_classes(ctx)
    (run_dir / f"css_classes_{name}.json").write_text(
        json.dumps(classes, indent=2), encoding="utf-8"
    )

    probe: Dict[str, int] = {}
    for sel in candidate_selectors:
        try:
            probe[sel] = ctx.locator(sel).count()
        except Exception:
            probe[sel] = -1
    (run_dir / f"selector_probe_{name}.json").write_text(
        json.dumps(probe, indent=2), encoding="utf-8"
    )


# ── Config file helpers ──────────────────────────────────────────────────────

def load_selectors_config(output_dir: Path) -> Dict[str, List[str]]:
    """Load optional order_selectors.json: { "purchase": [...], "completed": [...] }."""
    path = output_dir / SELECTORS_CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for key in ("purchase", "completed"):
        v = data.get(key)
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[key] = list(v)
    return out


def merge_selectors(
    builtin: List[str],
    config: Dict[str, List[str]],
    key: str,
) -> List[str]:
    """User-config selectors first, then builtins (dedupe, preserve order)."""
    extra = config.get(key) or []
    seen: set[str] = set()
    merged: List[str] = []
    for s in extra + builtin:
        if s not in seen:
            seen.add(s)
            merged.append(s)
    return merged


def gwt_soft_wait_after_action(
    ctx: Union[Page, Frame], *, settle_ms: int = 3000
) -> None:
    """Avoid relying on networkidle (GWT often never settles)."""
    try:
        ctx.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    try:
        ctx.wait_for_load_state("load", timeout=15_000)
    except Exception:
        pass
    ctx.wait_for_timeout(settle_ms)


# ── CLI (standalone probe listing) ───────────────────────────────────────────

if __name__ == "__main__":
    print("Orders UI probe selectors\n")
    print("Purchase top-menu tab:")
    for s in PURCHASE_NAV_SELECTORS:
        print(f"  {s}")
    print("\nOrders > PO > Completed (sidebar, post-Purchase-click only):")
    for s in ORDERS_COMPLETED_SELECTORS:
        print(f"  {s}")
    print("\nPost-Purchase-click page probe:")
    for s in POST_PURCHASE_PROBE_SELECTORS:
        print(f"  {s}")
    print("\nList page verification:")
    for s in LIST_PROBE_SELECTORS:
        print(f"  {s}")
    print(f"\nAll probe selectors: {len(all_probe_selectors())}")
    print(f"Post-purchase probe selectors: {len(post_purchase_probe_selectors())}")
