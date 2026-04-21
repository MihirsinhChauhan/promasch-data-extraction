"""
Fast parallel PDF downloader using requests + ThreadPoolExecutor.

PDFs are publicly accessible (no auth needed):
  https://gw.promasch.in/BillPdf?orderType={PO|WO}&billId={id}
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

import config
from utils import setup_logging, save_failed_id, validate_pdf, ProgressTracker

log = setup_logging("pdf_downloader")

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (PromaschExtractor/1.0)",
})


def download_pdf(bill_id: int, order_type: str) -> Optional[bytes]:
    """
    Download a single PDF. Returns raw bytes on success, None on failure.
    Validates HTTP status, content-type header, and %PDF- magic bytes.
    """
    url = f"{config.PDF_BASE_URL}?orderType={order_type}&billId={bill_id}"

    last_error = None
    for attempt in range(1, config.PDF_RETRIES + 1):
        try:
            if config.REQUEST_DELAY > 0:
                time.sleep(config.REQUEST_DELAY)

            resp = _session.get(url, timeout=config.PDF_TIMEOUT)

            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                log.warning(
                    "%s/%d attempt %d: %s", order_type, bill_id, attempt, last_error,
                )
                time.sleep(1 * attempt)
                continue

            content = resp.content
            is_valid, reason = validate_pdf(content)
            if is_valid:
                return content

            last_error = reason
            log.warning(
                "%s/%d attempt %d: %s", order_type, bill_id, attempt, last_error,
            )
            time.sleep(1 * attempt)

        except requests.exceptions.Timeout:
            last_error = "Timeout"
            log.warning("%s/%d attempt %d: timeout", order_type, bill_id, attempt)
            time.sleep(2 * attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = f"ConnectionError: {e}"
            log.warning("%s/%d attempt %d: connection error", order_type, bill_id, attempt)
            time.sleep(3 * attempt)
        except Exception as e:
            last_error = str(e)
            log.warning("%s/%d attempt %d: %s", order_type, bill_id, attempt, e)
            time.sleep(1 * attempt)

    save_failed_id(config.FAILED_IDS_FILE, order_type, bill_id, last_error or "Unknown")
    return None


def download_batch(
    bill_ids: list[int],
    order_type: str,
    max_workers: int | None = None,
    callback=None,
) -> dict[int, bytes]:
    """
    Download PDFs in parallel. Returns {bill_id: pdf_bytes} for successful downloads.

    callback(bill_id, success) is called after each download if provided.
    """
    if max_workers is None:
        max_workers = config.MAX_WORKERS

    results: dict[int, bytes] = {}
    tracker = ProgressTracker(len(bill_ids), label=f"{order_type} Download")

    log.info("Downloading %d %s PDFs with %d workers", len(bill_ids), order_type, max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(download_pdf, bid, order_type): bid
            for bid in bill_ids
        }

        for future in as_completed(future_to_id):
            bill_id = future_to_id[future]
            try:
                pdf_bytes = future.result()
                if pdf_bytes:
                    results[bill_id] = pdf_bytes
                    tracker.tick(success=True)
                else:
                    tracker.tick(success=False)

                if callback:
                    callback(bill_id, pdf_bytes is not None)

            except Exception as e:
                log.error("Unexpected error for %s/%d: %s", order_type, bill_id, e)
                tracker.tick(success=False)
                save_failed_id(config.FAILED_IDS_FILE, order_type, bill_id, str(e))

    tracker.close()
    log.info("Download complete: %s", tracker.summary_line())
    return results
