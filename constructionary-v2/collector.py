"""
Playwright collector v2: login, walk the Constructionary category tree,
intercept bulk getPartDetails RPC responses, parse display names, and
write parts_index.jsonl.  Also captures one getStockForStockroom request
payload as rpc_template.json so detail_client.py can replay it per-part.

Output artifacts (all under DATA_DIR):
  auth_state.json          — Playwright storage-state (cookies + localStorage)
  rpc_template.json        — First captured getStockForStockroom payload + meta
  bulk_dumps/              — Raw getPartDetails response bodies
  parts_index.jsonl        — One line per part: {display_name, category_path,
                             entity_ref, source_dump}
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page, sync_playwright

import config
from utils import append_jsonl, display_name_key, load_jsonl, setup_logging

log = setup_logging("collector")

# ---------------------------------------------------------------------------
# Display-name regex (same as v1 gwt_parser.py — brand never contains '(')
# ---------------------------------------------------------------------------
DISPLAY_NAME_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")

# ---------------------------------------------------------------------------
# DOM selectors
# ---------------------------------------------------------------------------
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

# Candidate selectors for individual part tiles in the detail panel.
# Used to trigger a getStockForStockroom click for template capture.
_PART_TILE_SELECTORS = [
    ".part_details_outer_css",   # confirmed via DevTools inspection
    ".partTileNew",
    ".partItem",
    ".partTile",
    "[class*='part_details']",
    "[class*='partTile']",
    "[class*='partItem']",
    "[class*='PartItem']",
    "[class*='partRow']",
    "[class*='PartRow']",
    "[class*='stockItem']",
]

_USER_INPUT_SEL = (
    'input[type="text"], input[type="email"], input[name*="user"], '
    'input[name*="email"], input[id*="user"], input[id*="email"], '
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="checkbox"])'
)

# ---------------------------------------------------------------------------
# GWT response normalization (inline minimal version)
# ---------------------------------------------------------------------------
_CONCAT_RE = re.compile(r"\]\s*\.concat\s*\(")


def _collapse_concat_arrays(t: str) -> str:
    while True:
        m = _CONCAT_RE.search(t)
        if not m:
            break
        j = m.end()
        n = len(t)
        parts: list[str] = []
        while j < n:
            while j < n and t[j] in " \t\n\r,":
                j += 1
            if j >= n:
                break
            if t[j] == ")":
                j += 1
                break
            if t[j] == "[":
                j += 1
                start = j
                depth = 1
                in_sq = in_dq = False
                while j < n and depth > 0:
                    ch = t[j]
                    if in_sq:
                        if ch == "'":
                            in_sq = False
                    elif in_dq:
                        if ch == '"':
                            in_dq = False
                    elif ch == "'":
                        in_sq = True
                    elif ch == '"':
                        in_dq = True
                    elif ch == "[":
                        depth += 1
                    elif ch == "]":
                        depth -= 1
                    j += 1
                parts.append(t[start : j - 1])
            else:
                start = j
                while j < n and t[j] not in ",)":
                    j += 1
                parts.append(t[start:j])
        t = t[: m.start()] + "," + ",".join(parts) + "]" + t[j:]
    return t


def _normalize_gwt(text: str) -> list[Any]:
    t = text.strip()
    if t.startswith("//EX"):
        raise ValueError(f"GWT exception: {t[:200]}")
    if t.startswith("//OK"):
        t = t[4:].strip()
    t = _collapse_concat_arrays(t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        import json5  # type: ignore
        return json5.loads(t)


def _find_string_table(data: list[Any]) -> Optional[list[str]]:
    """Find the largest all-string sub-list — that's the GWT string table."""
    best: Optional[list[str]] = None
    for el in data:
        if isinstance(el, list) and len(el) >= 2 and all(isinstance(x, str) for x in el):
            if best is None or len(el) > len(best):
                best = [str(x) for x in el]
    return best


def extract_display_names_from_response(body: str) -> list[str]:
    """Return all display names found in a raw getPartDetails GWT response."""
    try:
        data = _normalize_gwt(body)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    st = _find_string_table(data)
    if not st:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for s in st:
        if isinstance(s, str) and DISPLAY_NAME_RE.match(s.strip()) and s not in seen:
            seen.add(s)
            found.append(s.strip())
    return found


# ---------------------------------------------------------------------------
# GWT RPC template building for getStockForStockroom
# ---------------------------------------------------------------------------

def build_rpc_payload(template_payload: str, old_entity_ref: str, new_entity_ref: str) -> str:
    """Replace the entity_ref string in a captured GWT-RPC payload."""
    parts = template_payload.split("|")
    replaced = False
    for i, part in enumerate(parts):
        if part == old_entity_ref:
            parts[i] = new_entity_ref
            replaced = True
            break
    if not replaced:
        raise ValueError(
            f"entity_ref {old_entity_ref!r} not found in template payload. "
            "Re-capture the template or inspect rpc_template.json."
        )
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Login & navigation helpers
# ---------------------------------------------------------------------------

def login_and_open_constructionary(page: Page, base_url: str, user: str, password: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=120_000)
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


def first_match(ctx, selectors: list[str]):
    for sel in selectors:
        try:
            loc = ctx.locator(sel)
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, None


def clean(text: str) -> str:
    return " ".join(text.split()).strip()


# ---------------------------------------------------------------------------
# Scroll helpers
# ---------------------------------------------------------------------------

_BASE_SCROLL_ROUNDS = 30
_MAX_SCROLL_ROUNDS = 800
_IDLE_ROUNDS_THRESHOLD = 8
_PARTS_PER_RPC = 10


def _build_scroll_js(delta_y: int = 1800) -> str:
    selectors_js = json.dumps(_PARTS_PANEL_SELECTORS)
    return f"""
(function() {{
    var selectors = {selectors_js};
    for (var i = 0; i < selectors.length; i++) {{
        var el = document.querySelector(selectors[i]);
        if (el) {{ el.scrollBy(0, {delta_y}); return selectors[i]; }}
    }}
    var divs = Array.from(document.querySelectorAll('div'));
    var best = null, bestH = 0;
    divs.forEach(function(d) {{
        if (d.scrollHeight > d.clientHeight + 50 && d.clientHeight > bestH) {{
            var rect = d.getBoundingClientRect();
            if (rect.left > 200) {{ best = d; bestH = d.clientHeight; }}
        }}
    }});
    if (best) {{ best.scrollBy(0, {delta_y}); return 'auto:' + best.className.slice(0, 40); }}
    return null;
}})()
"""


def is_leaf_by_name(name: str) -> bool:
    low = name.lower()
    if re.search(r"\d+\s+categor", low):
        return False
    if re.search(r"\d+\s+specification", low) or re.search(r"\d+\s+parts?", low):
        return True
    return False


def parse_expected_parts(name: str) -> int:
    m = re.search(r"(\d+)\s+parts?", name, re.I)
    return int(m.group(1)) if m else 0


def adaptive_scroll_rounds(expected: int, base: int = _BASE_SCROLL_ROUNDS) -> int:
    if expected <= 0:
        return base
    return min(max(base, expected // _PARTS_PER_RPC + 10), _MAX_SCROLL_ROUNDS)


# ---------------------------------------------------------------------------
# Template capture — click a part tile in the panel to trigger detail RPC
# ---------------------------------------------------------------------------

def _try_click_part_for_template(ctx, state: dict) -> bool:
    """Try clicking the first visible part tile to trigger getStockForStockroom."""
    if state.get("template_captured"):
        return True
    for sel in _PART_TILE_SELECTORS:
        try:
            loc = ctx.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=5000)
                ctx.wait_for_timeout(2000)
                if state.get("template_captured"):
                    log.info("[template] Captured via %s", sel)
                    return True
        except Exception:
            continue
    return state.get("template_captured", False)


# ---------------------------------------------------------------------------
# Main collection walk
# ---------------------------------------------------------------------------

def walk_and_collect(
    ctx,
    tree_sel: str,
    *,
    max_folders: int,
    scroll_rounds: int,
    state: dict,
    parts_index_path: Path,
    bulk_dumps_dir: Path,
    folder_start: int = 0,
    folder_limit: int = 0,
    limit: Optional[int] = None,
) -> None:
    """Walk the tree, trigger bulk RPCs, write parts_index.jsonl."""
    visited: set[str] = set()
    i = 0
    leaf_count = 0
    # Track only NEW parts written this session so --limit counts fresh writes,
    # not the pre-existing resume baseline.
    parts_written_start = state["parts_written"]
    total_parts_written = 0

    scroll_js = _build_scroll_js()

    while i < max_folders:
        if limit is not None and total_parts_written >= limit:
            log.info("[walk] Part limit %d new parts reached — stopping.", limit)
            break

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

        if not is_leaf:
            state["current_folder"] = name
            state["current_category"] = ""
            log.info("[node %d] FOLDER: %s", i, name)
        else:
            state["current_category"] = name
            if leaf_count < folder_start:
                leaf_count += 1
                log.info("[node %d] SKIP leaf %d/%d: %s", i, leaf_count, folder_start, name)
                continue
            log.info("[node %d] LEAF #%d: %s", i, leaf_count + 1, name)

        try:
            folder.click(timeout=8000)
        except Exception as e:
            log.warning("[node %d] click failed: %s", i, e)
            continue

        try:
            ctx.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        ctx.wait_for_timeout(1200)

        if not is_leaf:
            continue

        leaf_count += 1
        prev_parts = total_parts_written

        # Try to capture template via part tile click (only needed once)
        if not state.get("template_captured"):
            ctx.wait_for_timeout(800)
            _try_click_part_for_template(ctx, state)

        # Scroll to trigger pagination
        matched_sel = None
        try:
            matched_sel = ctx.evaluate(scroll_js)
        except Exception:
            pass

        if not matched_sel:
            ctx.mouse.move(900, 450)

        expected = parse_expected_parts(name)
        rounds = adaptive_scroll_rounds(expected, base=scroll_rounds)
        if expected > 0:
            log.debug("[scroll] %s: expected %d parts → %d rounds", name, expected, rounds)

        prev_rpc = state["bulk_rpc_count"]
        idle_rounds = 0
        last_rpc = state["bulk_rpc_count"]

        for _ in range(rounds):
            total_parts_written = state["parts_written"] - parts_written_start
            if limit is not None and total_parts_written >= limit:
                break
            ctx.wait_for_timeout(1200)
            if matched_sel:
                try:
                    ctx.evaluate(scroll_js)
                except Exception:
                    ctx.mouse.wheel(0, 1800)
            else:
                ctx.mouse.wheel(0, 1800)

            total_parts_written = state["parts_written"] - parts_written_start
            if state["bulk_rpc_count"] == last_rpc:
                idle_rounds += 1
                if idle_rounds >= _IDLE_ROUNDS_THRESHOLD:
                    break
            else:
                idle_rounds = 0
                last_rpc = state["bulk_rpc_count"]

        new_rpcs = state["bulk_rpc_count"] - prev_rpc
        new_parts = (state["parts_written"] - parts_written_start) - prev_parts
        log.info("[scroll] %s: %d RPC(s), %d part refs written", name, new_rpcs, new_parts)

        total_parts_written = state["parts_written"] - parts_written_start

        if folder_limit > 0 and leaf_count >= folder_start + folder_limit:
            log.info("[walk] Folder limit %d reached — stopping.", folder_limit)
            break


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_collection(
    *,
    data_dir: Path,
    base_url: str,
    user: str,
    password: str,
    headful: bool,
    max_folders: int,
    scroll_rounds: int,
    tree_sel_override: Optional[str] = None,
    folder_start: int = 0,
    folder_limit: int = 0,
    limit: Optional[int] = None,
    resume: bool = False,
) -> int:
    """Perform the enumeration phase.  Returns total part refs written."""
    parts_index_path = data_dir / "parts_index.jsonl"
    bulk_dumps_dir = data_dir / "bulk_dumps"
    auth_state_path = data_dir / "auth_state.json"
    rpc_template_path = data_dir / "rpc_template.json"

    bulk_dumps_dir.mkdir(parents=True, exist_ok=True)

    # Resume: build set of already-indexed display names
    existing: set[str] = set()
    if resume and parts_index_path.exists():
        for rec in load_jsonl(parts_index_path):
            if rec.get("display_name"):
                existing.add(rec["display_name"])
        log.info("[collect] Resume: %d display names already in index", len(existing))

    state: dict[str, Any] = {
        "bulk_rpc_count": 0,
        "parts_written": len(existing),
        "current_folder": "",
        "current_category": "",
        "template_captured": rpc_template_path.exists(),
    }

    def on_response(response) -> None:
        try:
            req = response.request
            if req.method != "POST":
                return
            url = req.url
            if "deptherp/erp" not in url and "ERPService" not in url:
                return
            pd = req.post_data or ""

            # ── Bulk enumeration RPC ────────────────────────────────────────
            if "getPartDetails" in pd:
                if response.status != 200:
                    return
                body = response.text()
                state["bulk_rpc_count"] += 1
                stem = f"{int(time.time() * 1000)}_{state['bulk_rpc_count']}"
                dump_path = bulk_dumps_dir / f"{stem}.txt"
                dump_path.write_text(body, encoding="utf-8")

                names = extract_display_names_from_response(body)
                category_path = [
                    state["current_folder"],
                    state["current_category"],
                ]
                category_path = [p for p in category_path if p]

                for name in names:
                    if name in existing:
                        continue
                    existing.add(name)
                    append_jsonl(
                        parts_index_path,
                        {
                            "display_name": name,
                            "entity_ref": name,
                            "category_path": category_path,
                            "source_dump": str(dump_path.relative_to(data_dir)),
                        },
                    )
                    state["parts_written"] += 1

            # ── Detail template capture ─────────────────────────────────────
            elif "getStockForStockroom" in pd and not state["template_captured"]:
                if response.status != 200:
                    return
                # Identify the entity_ref in the payload: last pipe-segment
                # that matches DISPLAY_NAME_RE
                segments = pd.split("|")
                entity_ref: Optional[str] = None
                for seg in reversed(segments):
                    if DISPLAY_NAME_RE.match(seg.strip()):
                        entity_ref = seg.strip()
                        break

                # Extract X-GWT-Permutation from headers
                gwt_perm = req.headers.get("x-gwt-permutation") or ""

                rpc_template_path.write_text(
                    json.dumps(
                        {
                            "url": url,
                            "payload": pd,
                            "entity_ref": entity_ref,
                            "gwt_permutation": gwt_perm,
                            "headers": dict(req.headers),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                state["template_captured"] = True
                log.info(
                    "[template] Saved getStockForStockroom template (entity_ref=%s)",
                    entity_ref,
                )

        except Exception as exc:
            log.debug("[on_response] error: %s", exc)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()
        page.on("response", on_response)

        log.info("[collect] Logging in to %s", base_url)
        login_and_open_constructionary(page, base_url, user, password)
        ctx = get_context(page)

        tree_sel = tree_sel_override
        if not tree_sel:
            _, tree_sel = first_match(ctx, _TREE_SELECTORS)

        if not tree_sel:
            log.error("[collect] No tree selector matched — try --sel-tree")
        else:
            log.info("[collect] Using tree selector: %s", tree_sel)
            walk_and_collect(
                ctx,
                tree_sel,
                max_folders=max_folders,
                scroll_rounds=scroll_rounds,
                state=state,
                parts_index_path=parts_index_path,
                bulk_dumps_dir=bulk_dumps_dir,
                folder_start=folder_start,
                folder_limit=folder_limit,
                limit=limit,
            )

        # Save auth state for detail_client session cookies
        context.storage_state(path=str(auth_state_path))
        log.info("[collect] Saved auth state → %s", auth_state_path)
        browser.close()

    total = state["parts_written"]
    log.info("[collect] Done. Total part refs in index: %d", total)

    if not state["template_captured"]:
        log.warning(
            "[collect] getStockForStockroom template NOT captured. "
            "Re-run with --headful and click on a part tile, or capture manually."
        )

    return total
