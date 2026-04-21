"""
Parallel PDF downloader for Indents.

Unlike vendor bills/orders, many indent records do not expose a direct PDF URL.
This downloader accepts an explicit {indent_id: pdf_url} map and skips IDs where
URL is missing.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

import config
from utils import ProgressTracker, save_failed_id, setup_logging, validate_pdf

log = setup_logging("pdf_downloader")

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0 (PromaschExtractor/1.0)"})


def download_pdf(indent_id: str, pdf_url: str) -> Optional[bytes]:
    last_error: Optional[str] = None

    for attempt in range(1, config.PDF_RETRIES + 1):
        try:
            if config.REQUEST_DELAY > 0:
                time.sleep(config.REQUEST_DELAY)

            resp = _session.get(pdf_url, timeout=config.PDF_TIMEOUT)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                log.warning("indent/%s attempt %d: %s", indent_id, attempt, last_error)
                time.sleep(attempt)
                continue

            is_valid, reason = validate_pdf(resp.content)
            if is_valid:
                return resp.content

            last_error = reason
            log.warning("indent/%s attempt %d: %s", indent_id, attempt, reason)
            time.sleep(attempt)

        except requests.exceptions.Timeout:
            last_error = "Timeout"
            log.warning("indent/%s attempt %d: timeout", indent_id, attempt)
            time.sleep(2 * attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = f"ConnectionError: {e}"
            log.warning("indent/%s attempt %d: connection error", indent_id, attempt)
            time.sleep(3 * attempt)
        except Exception as e:
            last_error = str(e)
            log.warning("indent/%s attempt %d: %s", indent_id, attempt, e)
            time.sleep(attempt)

    save_failed_id(config.FAILED_IDS_FILE, indent_id, last_error or "Unknown")
    return None


def download_batch(
    url_map: dict[str, str],
    *,
    max_workers: Optional[int] = None,
    callback=None,
) -> dict[str, bytes]:
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    valid_items = [(indent_id, url) for indent_id, url in url_map.items() if url]
    skipped = len(url_map) - len(valid_items)
    if skipped > 0:
        log.info("Skipping %d indent(s) with no pdf_url", skipped)

    if not valid_items:
        return {}

    tracker = ProgressTracker(len(valid_items), label="Indent PDF Download")
    results: dict[str, bytes] = {}
    log.info("Downloading %d indent PDFs with %d workers", len(valid_items), max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(download_pdf, indent_id, url): indent_id
            for indent_id, url in valid_items
        }

        for future in as_completed(future_to_id):
            indent_id = future_to_id[future]
            try:
                pdf_bytes = future.result()
                ok = pdf_bytes is not None
                if ok:
                    results[indent_id] = pdf_bytes
                tracker.tick(success=ok)
                if callback:
                    callback(indent_id, ok)
            except Exception as e:
                log.error("Unexpected error for indent/%s: %s", indent_id, e)
                tracker.tick(success=False)
                save_failed_id(config.FAILED_IDS_FILE, indent_id, str(e))

    tracker.close()
    log.info("Download complete: %s", tracker.summary_line())
    return results
