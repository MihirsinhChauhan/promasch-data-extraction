"""
Playwright-based scraper for vendor bill metadata.

Navigates the Promasch GWT UI, handles infinite scroll, and extracts
bill records by regex-parsing the visible page text (GWT renders cards,
not standard table rows).
"""

import json
import re
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

import config
from utils import setup_logging, save_json, load_json

log = setup_logging("scraper")

# ── Regex patterns for parsing bill card text blocks ─────────────────────────

# Splits the full page text into one chunk per bill.
# Each chunk starts with "PO BILL NO." or "WO BILL NO."
_BILL_SPLIT = re.compile(r"(?=(?:PO|WO) BILL NO\.)")

# Field extractors applied to each chunk
_RE_BILL_NO = re.compile(
    r"(?:PO|WO) BILL NO\.\s*(.+?)\s*\|\s*INVOICE NO\.\s*(\S+)"
)
_RE_VENDOR = re.compile(r"VENDOR\s+V\s+(.+?)(?=\s*ENTITY\s)")
_RE_ENTITY = re.compile(r"ENTITY\s+E\s+(.+?)(?=\s*INVOICE DATE)")
_RE_INV_DATE = re.compile(r"INVOICE DATE\s+(\d{1,2}-\w{3}-\d{4})")
_RE_ENTERED = re.compile(r"ENTERED BY\s+(.+?)(?=\s*LOCATION)")
_RE_LOCATION = re.compile(r"LOCATION\s+\(\d+\)\s+(.+?)(?=\s*ITEM CATEGORIES)")
_RE_CATEGORIES = re.compile(
    r"ITEM CATEGORIES\s+\(\d+\)\s+(.+?)(?=\s*BILL/INVOICE STATUS)"
)
_RE_TOTAL = re.compile(r"TOTAL VALUE\s+₹\s*([\d,\.]+)")
_RE_PO_COUNT = re.compile(r"(?:PO|WO) NOs:\s*(\d+)")
_RE_BILL_ID = re.compile(r"(\d+)\s*$")


def _login(page: Page) -> None:
    log.info("Logging in as %s ...", config.LOGIN_USER)
    page.goto(config.BASE_URL, wait_until="domcontentloaded", timeout=120_000)
    page.fill('input[type="text"]', config.LOGIN_USER)
    page.fill('input[type="password"]', config.LOGIN_PASSWORD)
    page.click(
        'button[type="submit"], button:has-text("Login"), input[type="submit"]'
    )
    page.wait_for_load_state("networkidle", timeout=120_000)
    log.info("Login successful")


def _navigate_to_vendor_bills(page: Page, bill_type: str) -> None:
    log.info("Navigating to %s Bills ...", bill_type)
    page.locator("text=Purchase").first.click()
    page.wait_for_timeout(2000)
    page.locator("text=Vendor Bills").first.click()
    page.wait_for_timeout(3000)
    tab_text = f"{bill_type} Bills"
    page.locator(f"text={tab_text}").first.click()
    page.wait_for_timeout(3000)
    log.info("Navigated to %s Bills tab", bill_type)


def _get_ui_context(page: Page):
    """Return the frame/page that holds the bill content."""
    for frame in page.frames:
        try:
            if frame.locator("text=BILL NO").count() > 0:
                return frame
            if frame.locator("text=Bill No").count() > 0:
                return frame
        except Exception:
            continue
    return page


def _clean(text: str) -> str:
    return " ".join(text.split()).strip()


def _parse_bill_block(block: str, bill_type: str) -> Optional[dict]:
    """Parse a single bill text block into a structured record."""
    m = _RE_BILL_NO.search(block)
    if not m:
        return None

    full_bill_no = _clean(m.group(1))
    invoice_no = _clean(m.group(2))

    # bill_id is the last number in the full bill number
    id_match = _RE_BILL_ID.search(full_bill_no)
    if not id_match:
        return None
    bill_id = int(id_match.group(1))

    def _extract(pattern: re.Pattern) -> str:
        hit = pattern.search(block)
        return _clean(hit.group(1)) if hit else ""

    return {
        "bill_id": bill_id,
        "type": bill_type,
        "bill_no": full_bill_no,
        "invoice_no": invoice_no,
        "vendor": _extract(_RE_VENDOR),
        "entity": _extract(_RE_ENTITY),
        "invoice_date": _extract(_RE_INV_DATE),
        "entered_by": _extract(_RE_ENTERED),
        "location": _extract(_RE_LOCATION),
        "item_categories": _extract(_RE_CATEGORIES),
        "total_value": _extract(_RE_TOTAL),
        "po_count": _extract(_RE_PO_COUNT),
    }


def _extract_bills_from_text(full_text: str, bill_type: str) -> list[dict]:
    """Split page text into bill blocks and parse each one."""
    # inner_text() returns newlines between elements; collapse to single line
    # so regex lookaheads like VENDOR...ENTITY work across what were DOM boundaries
    flat = " ".join(full_text.split())
    blocks = _BILL_SPLIT.split(flat)
    results = []
    for block in blocks:
        if not block.strip():
            continue
        record = _parse_bill_block(block, bill_type)
        if record:
            results.append(record)
    return results


_JS_FIND_SCROLLABLE = """() => {
    // Find the deepest scrollable container that holds bill content.
    // GWT wraps the list in a div with overflow:auto/scroll.
    const candidates = document.querySelectorAll('div, td');
    let best = null;
    let bestScore = 0;
    for (const el of candidates) {
        const style = getComputedStyle(el);
        const overY = style.overflowY;
        if (overY !== 'auto' && overY !== 'scroll') continue;
        if (el.scrollHeight <= el.clientHeight + 10) continue;
        // Score by scroll depth (larger = more content to scroll)
        const score = el.scrollHeight - el.clientHeight;
        if (score > bestScore) {
            bestScore = score;
            best = el;
        }
    }
    return best;  // null if nothing found
}"""

_JS_SCROLL_DOWN = """(el) => {
    el.scrollTop += 3000;
    return el.scrollTop;
}"""


def _scroll_and_extract(
    ctx,
    page: Page,
    bill_type: str,
    live_file: Path = None,
    checkpoint_file: Path = None,
) -> list[dict]:
    """
    Scroll the bill list, grab page text after each scroll, parse bill blocks,
    and deduplicate. Streams each new record to live_file as JSONL.

    Saves a checkpoint to checkpoint_file every 500 new records so that
    data survives crashes.
    """
    all_records: dict[int, dict] = {}
    stable_rounds = 0
    live_fh = open(live_file, "w", encoding="utf-8") if live_file else None
    max_scroll_rounds = 800
    last_checkpoint_count = 0

    # Find the scrollable container via JS
    scroll_handle = ctx.evaluate_handle(_JS_FIND_SCROLLABLE)
    use_js_scroll = scroll_handle.as_element() is not None
    if use_js_scroll:
        log.info("Found scrollable container via JS — using JS scroll")
    else:
        log.info("No scrollable container found — falling back to mouse wheel")

    for round_num in range(max_scroll_rounds):
        ctx.wait_for_timeout(config.SCROLL_PAUSE_MS)

        try:
            text = ctx.inner_text("body", timeout=15_000)
        except Exception:
            try:
                text = ctx.text_content("body", timeout=10_000) or ""
            except Exception:
                text = ""

        new_records = _extract_bills_from_text(text, bill_type)
        new_count = 0

        for rec in new_records:
            bid = rec["bill_id"]
            if bid not in all_records:
                all_records[bid] = rec
                new_count += 1
                if live_fh:
                    live_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    live_fh.flush()

        # Checkpoint save every 500 new records
        if checkpoint_file and len(all_records) - last_checkpoint_count >= 500:
            save_json(checkpoint_file, list(all_records.values()))
            last_checkpoint_count = len(all_records)
            log.info(
                "Checkpoint saved: %d records to %s",
                len(all_records), checkpoint_file.name,
            )

        if new_count == 0:
            stable_rounds += 1
            if stable_rounds >= config.SCROLL_STABLE_THRESHOLD:
                log.info(
                    "No new bills for %d rounds — done (%d total)",
                    stable_rounds, len(all_records),
                )
                break
        else:
            stable_rounds = 0

        if round_num % 10 == 0:
            log.info(
                "Round %d: %d total records (+%d new this round)",
                round_num, len(all_records), new_count,
            )

        # Scroll: prefer JS on the container, fallback to mouse wheel on page
        if use_js_scroll:
            ctx.evaluate(_JS_SCROLL_DOWN, scroll_handle)
        else:
            page.mouse.wheel(0, 3000)

    if live_fh:
        live_fh.close()

    # Final checkpoint
    if checkpoint_file:
        save_json(checkpoint_file, list(all_records.values()))

    return list(all_records.values())


def _recover_from_jsonl(jsonl_path: Path) -> list[dict]:
    """Load records from a JSONL crash-recovery file."""
    records: dict[int, dict] = {}
    if not jsonl_path.exists():
        return []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                bid = rec.get("bill_id")
                if bid is not None:
                    records[bid] = rec
            except json.JSONDecodeError:
                continue
    return list(records.values())


def scrape_bills(bill_type: str, headless: bool = True) -> list:
    """
    Full scrape pipeline for a bill type (PO or WO).
    Returns list of bill metadata dicts.

    Data safety:
      - Each record is streamed to {type}_bills_live.jsonl immediately
      - po_metadata.json is checkpoint-saved every 500 records
      - If a previous run crashed, existing data from the JSON or JSONL is loaded
    """
    output_file = (
        config.PO_METADATA_FILE if bill_type == "PO" else config.WO_METADATA_FILE
    )
    live_file = config.DATA_DIR / f"{bill_type.lower()}_bills_live.jsonl"

    # Load existing data from the main JSON file
    existing = load_json(output_file)

    # If main JSON is empty but JSONL has data from a crashed run, recover it
    if not existing and live_file.exists():
        recovered = _recover_from_jsonl(live_file)
        if recovered:
            log.info(
                "Recovered %d %s records from %s (previous crash?)",
                len(recovered), bill_type, live_file.name,
            )
            existing = recovered
            save_json(output_file, existing)

    if existing:
        log.info(
            "Found %d existing %s records — will merge new ones",
            len(existing), bill_type,
        )
        existing_ids = {r["bill_id"] for r in existing}
    else:
        existing_ids = set()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless)
        except PlaywrightError as e:
            if "Executable doesn't exist" in str(e):
                log.error(
                    "Playwright browser binaries missing. Run:\n"
                    "  python -m playwright install chromium"
                )
            raise
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        try:
            _login(page)
            _navigate_to_vendor_bills(page, bill_type)
            ctx = _get_ui_context(page)

            log.info("Streaming rows to %s (tail -f to watch)", live_file)

            rows = _scroll_and_extract(
                ctx, page, bill_type,
                live_file=live_file,
                checkpoint_file=output_file,
            )
            log.info("Extracted %d %s bill rows from UI", len(rows), bill_type)

            new_rows = [r for r in rows if r["bill_id"] not in existing_ids]
            log.info("New rows (not in existing data): %d", len(new_rows))

            all_records = existing + new_rows
            save_json(output_file, all_records)
            log.info(
                "Saved %d total %s records to %s",
                len(all_records), bill_type, output_file,
            )

        except Exception as e:
            log.error("Scraper error for %s: %s", bill_type, e, exc_info=True)
            raise
        finally:
            browser.close()

    return load_json(output_file)


def scrape_all(headless: bool = True) -> dict:
    """Scrape both PO and WO bills. Returns {"po": [...], "wo": [...]}."""
    return {
        "po": scrape_bills("PO", headless=headless),
        "wo": scrape_bills("WO", headless=headless),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["PO", "WO", "ALL"], default="ALL")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    if args.type == "ALL":
        result = scrape_all(headless=not args.headful)
        log.info(
            "PO: %d records, WO: %d records",
            len(result["po"]), len(result["wo"]),
        )
    else:
        records = scrape_bills(args.type, headless=not args.headful)
        log.info("%s: %d records", args.type, len(records))
