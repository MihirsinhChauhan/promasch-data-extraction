"""
Probe likely indent PDF URL patterns and detect a working template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import requests

import config
from utils import load_json, save_json, setup_logging, validate_pdf

log = setup_logging("pdf_probe")


def default_candidate_templates(base_url: str) -> list[str]:
    return [
        f"{base_url}/IndentPdf?indentId={{indent_id}}",
        f"{base_url}/IndentPdf?indentId={{indent_id}}&showPrice=1",
        f"{base_url}/IndentPdf?indentNo={{indent_id}}",
        f"{base_url}/indentPdf?indentId={{indent_id}}",
        f"{base_url}/PrintIndent?indentId={{indent_id}}",
        f"{base_url}/BillPdf?orderType=INDENT&billId={{indent_id}}",
        f"{base_url}/BillPdf?orderType=IN&billId={{indent_id}}",
    ]


@dataclass
class ProbeResult:
    template: str
    tested: int
    success: int
    sample_success_urls: list[str]
    sample_errors: list[str]


def probe_templates(
    indent_ids: Iterable[str],
    *,
    candidates: Optional[list[str]] = None,
    timeout: Optional[int] = None,
    retries: Optional[int] = None,
) -> tuple[Optional[str], list[ProbeResult]]:
    ids = [str(x) for x in indent_ids if str(x).strip()]
    if not ids:
        return None, []

    candidates = candidates or default_candidate_templates(config.BASE_URL)
    timeout = timeout or config.PDF_TIMEOUT
    retries = retries or max(1, config.PDF_RETRIES)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (PromaschExtractor/1.0)"})

    results: list[ProbeResult] = []

    for template in candidates:
        tested = 0
        success = 0
        sample_success_urls: list[str] = []
        sample_errors: list[str] = []

        for indent_id in ids:
            tested += 1
            url = template.format(indent_id=indent_id)
            last_error = "Unknown error"

            for _ in range(retries):
                try:
                    resp = session.get(url, timeout=timeout)
                    if resp.status_code != 200:
                        last_error = f"{indent_id}: HTTP {resp.status_code}"
                        continue
                    ok, reason = validate_pdf(resp.content)
                    if ok:
                        success += 1
                        if len(sample_success_urls) < 3:
                            sample_success_urls.append(url)
                        last_error = ""
                        break
                    last_error = f"{indent_id}: {reason}"
                except Exception as e:
                    last_error = f"{indent_id}: {e}"

            if last_error and len(sample_errors) < 5:
                sample_errors.append(last_error)

        results.append(ProbeResult(
            template=template,
            tested=tested,
            success=success,
            sample_success_urls=sample_success_urls,
            sample_errors=sample_errors,
        ))

    results.sort(key=lambda x: (x.success, -x.tested), reverse=True)
    best = results[0].template if results and results[0].success > 0 else None
    return best, results


def backfill_catalog_pdf_urls(template: str) -> int:
    catalog = load_json(config.INDENT_CATALOG_FILE)
    if not isinstance(catalog, list):
        return 0

    updated = 0
    for row in catalog:
        indent_id = str(row.get("indent_id") or "")
        if not indent_id:
            continue
        if row.get("pdf_url"):
            continue
        row["pdf_url"] = template.format(indent_id=indent_id)
        updated += 1

    if updated:
        save_json(config.INDENT_CATALOG_FILE, catalog)
    return updated
