"""
Indent UI probing (Constructionary-style parity).

Provides frame detection, selector snapshots (HTML + css_classes + probe counts),
first_match over candidate lists, and optional indent_selectors.json overrides.

See UI-extraction/main.py save_snapshot / first_match patterns.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from playwright.sync_api import Frame, Locator, Page

# ── Frame markers (regex text locators; try main page then iframes) ────────────

_INDENT_UI_MARKER_LOCS = [
    "text=/INDENTS/i",
    "text=/Purchase/i",
    "text=/Search Indent Number/i",
    "text=/IN-PROCESS/i",
]

# ── Default candidate selectors (probed + used by collector) ─────────────────

# GWT top bar: <td class="gwt-MenuItem" role="menuitem" title="Purchase">, not <a>.
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

COMPLETED_FALLBACK_SELECTORS: List[str] = [
    "div:has-text('INDENTS') >> text=/COMPLETED\\s*\\|\\s*\\d+/",
    "div:has-text('INDENTS') >> text=COMPLETED",
    "text=INDENTS >> xpath=ancestor::*[1] >> text=COMPLETED",
]

LIST_PROBE_SELECTORS: List[str] = [
    "text=/INDENT NO\\./i",
    "text=Search Indent Number here",
    "text=Completed",
    "div:has-text('INDENT NO.')",
]

INDENT_SELECTORS_CONFIG_NAME = "indent_selectors.json"


def all_probe_selectors() -> List[str]:
    """Single list for combined snapshot probe (deduped, stable order)."""
    seen: set[str] = set()
    out: List[str] = []
    for s in PURCHASE_NAV_SELECTORS + COMPLETED_FALLBACK_SELECTORS + LIST_PROBE_SELECTORS:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


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


def get_indent_ui_context(page: Page) -> Union[Page, Frame]:
    """Return the frame (or main page) that contains Indent / Purchase UI markers."""
    for sel in _INDENT_UI_MARKER_LOCS:
        try:
            if page.locator(sel).count() > 0:
                return page
        except Exception:
            continue

    for frame in page.frames:
        if not frame.url:
            continue
        try:
            for sel in _INDENT_UI_MARKER_LOCS:
                if frame.locator(sel).count() > 0:
                    return frame
        except Exception:
            continue

    return page


# Elements that indicate the main ERP chrome (not the login form). Login may still
# expose some gwt-* widgets; td.gwt-MenuItem / MenuBar are the usual post-auth bar.
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
    """Poll main page and frames until GWT menu chrome appears.

    Returns the Page or Frame where the menu appeared (use for snapshots/locators),
    or None on timeout.
    """
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


def load_indent_selectors_config(output_dir: Path) -> Dict[str, List[str]]:
    """Load optional indent_selectors.json: { \"purchase\": [...], \"completed\": [...] }."""
    path = output_dir / INDENT_SELECTORS_CONFIG_NAME
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


def merge_purchase_selectors(
    builtin: List[str],
    config: Dict[str, List[str]],
) -> List[str]:
    """User purchase selectors first, then builtins (dedupe, preserve order)."""
    extra = config.get("purchase") or []
    seen: set[str] = set()
    merged: List[str] = []
    for s in extra + builtin:
        if s not in seen:
            seen.add(s)
            merged.append(s)
    return merged


def merge_completed_selectors(
    builtin: List[str],
    config: Dict[str, List[str]],
) -> List[str]:
    """User completed fallback selectors first, then builtins."""
    extra = config.get("completed") or []
    seen: set[str] = set()
    merged: List[str] = []
    for s in extra + builtin:
        if s not in seen:
            seen.add(s)
            merged.append(s)
    return merged


def gwt_soft_wait_after_action(ctx: Union[Page, Frame], *, settle_ms: int = 3000) -> None:
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
