"""
Parallel PDF downloader for Orders PO PDFs.

PDF URL format:
  https://gw.promasch.in/OrderPdf?orderType=PO1&orderId={order_id}&Specs=true&Location=true&showPrice=1

The endpoint may require a valid session cookie.  If a plain GET returns an
HTML page instead of a PDF (auth redirect), the downloader will retry with
cookies extracted from auth_state.json (saved by the Playwright scrape phase).
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

import config
from utils import setup_logging, save_failed_id, validate_pdf, ProgressTracker

log = setup_logging("pdf_downloader")

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (PromaschExtractor/1.0)"})


def _load_auth_cookies(auth_path: Optional[Path] = None) -> dict[str, str]:
    """Load session cookies from auth_state.json if present."""
    if auth_path is None:
        auth_path = config.DATA_DIR / "auth_state.json"
    if not auth_path.is_file():
        return {}
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
        return {c["name"]: c["value"] for c in raw.get("cookies", [])}
    except Exception:
        return {}


def download_pdf(
    order_id: str,
    *,
    cookies: Optional[dict[str, str]] = None,
) -> Optional[bytes]:
    """
    Download a single order PDF.  Returns raw bytes on success, None on failure.

    Tries without cookies first; if the server returns HTML (auth redirect)
    and cookies are available, retries with them.
    """
    url = (
        f"{config.ORDER_PDF_URL}?orderType={config.ORDER_TYPE}"
        f"&orderId={order_id}&Specs=true&Location=true&showPrice=1"
    )

    last_error: Optional[str] = None

    for attempt in range(1, config.PDF_RETRIES + 1):
        try:
            if config.REQUEST_DELAY > 0:
                time.sleep(config.REQUEST_DELAY)

            resp = _session.get(
                url,
                timeout=config.PDF_TIMEOUT,
                cookies=cookies or {},
            )

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                log.warning("order/%s attempt %d: %s", order_id, attempt, last_error)
                time.sleep(attempt)
                continue

            content = resp.content
            is_valid, reason = validate_pdf(content)

            if is_valid:
                return content

            # HTML auth redirect — retry with cookies if we haven't already
            if "HTML error page" in reason and not cookies:
                log.debug("order/%s: got HTML, trying with auth cookies", order_id)
                auth_cookies = _load_auth_cookies()
                if auth_cookies:
                    resp2 = _session.get(url, timeout=config.PDF_TIMEOUT, cookies=auth_cookies)
                    if resp2.status_code == 200:
                        is_valid2, reason2 = validate_pdf(resp2.content)
                        if is_valid2:
                            return resp2.content
                        last_error = reason2
                    else:
                        last_error = f"HTTP {resp2.status_code} (with cookies)"
                    log.warning("order/%s attempt %d (with cookies): %s", order_id, attempt, last_error)
                    time.sleep(attempt)
                    continue

            last_error = reason
            log.warning("order/%s attempt %d: %s", order_id, attempt, last_error)
            time.sleep(attempt)

        except requests.exceptions.Timeout:
            last_error = "Timeout"
            log.warning("order/%s attempt %d: timeout", order_id, attempt)
            time.sleep(2 * attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = f"ConnectionError: {e}"
            log.warning("order/%s attempt %d: connection error", order_id, attempt)
            time.sleep(3 * attempt)
        except Exception as e:
            last_error = str(e)
            log.warning("order/%s attempt %d: %s", order_id, attempt, e)
            time.sleep(attempt)

    save_failed_id(config.FAILED_IDS_FILE, order_id, last_error or "Unknown")
    return None


def download_batch(
    order_ids: list[str],
    *,
    max_workers: Optional[int] = None,
    cookies: Optional[dict[str, str]] = None,
    callback=None,
) -> dict[str, bytes]:
    """
    Download PDFs in parallel.
    Returns {order_id: pdf_bytes} for successful downloads.
    """
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    # Lazily load auth cookies once for the whole batch
    if cookies is None:
        cookies = _load_auth_cookies() or None

    results: dict[str, bytes] = {}
    tracker = ProgressTracker(len(order_ids), label="Orders PDF Download")
    log.info(
        "Downloading %d order PDFs with %d workers",
        len(order_ids), max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(download_pdf, oid, cookies=cookies): oid
            for oid in order_ids
        }

        for future in as_completed(future_to_id):
            order_id = future_to_id[future]
            try:
                pdf_bytes = future.result()
                if pdf_bytes:
                    results[order_id] = pdf_bytes
                    tracker.tick(success=True)
                else:
                    tracker.tick(success=False)
                if callback:
                    callback(order_id, pdf_bytes is not None)
            except Exception as e:
                log.error("Unexpected error for order/%s: %s", order_id, e)
                tracker.tick(success=False)
                save_failed_id(config.FAILED_IDS_FILE, order_id, str(e))

    tracker.close()
    log.info("Download complete: %s", tracker.summary_line())
    return results
