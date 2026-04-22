"""
Detail RPC worker pool: calls getStockForStockroom for each part in
parts_index.jsonl, dumps the raw GWT response to
data/detail_dumps/{sha1(display_name)}.txt.

Resume-safe: skips any dump file that already exists (and has content).
Retries with exponential backoff on transient HTTP errors.

Template requirement:
  data/rpc_template.json must exist (written by collector.py).
  It contains {url, payload, entity_ref, gwt_permutation, headers}.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

import config
from session_promasch import load_cookie_jar_from_storage_state
from utils import (
    append_failed_detail,
    display_name_key,
    load_json,
    load_jsonl,
    setup_logging,
)

log = setup_logging("detail_client")

# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

def _build_rpc_payload(template_payload: str, old_entity_ref: str, new_entity_ref: str) -> str:
    """Replace the entity_ref string in a captured GWT-RPC v7 payload."""
    parts = template_payload.split("|")
    for i, part in enumerate(parts):
        if part == old_entity_ref:
            parts[i] = new_entity_ref
            return "|".join(parts)
    raise ValueError(
        f"entity_ref {old_entity_ref!r} not found in template payload.\n"
        "Re-capture the template or inspect data/rpc_template.json."
    )


def _build_session(auth_state_path: Optional[Path], gwt_permutation: str, referer: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
            "X-GWT-Permutation": gwt_permutation,
            "Referer": referer,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
        }
    )
    if auth_state_path and auth_state_path.is_file():
        s.cookies.update(load_cookie_jar_from_storage_state(auth_state_path))
    return s


# ---------------------------------------------------------------------------
# Single-part fetcher with retries
# ---------------------------------------------------------------------------

def fetch_detail(
    session: requests.Session,
    url: str,
    payload: str,
    *,
    max_retries: int = 3,
    timeout: float = 30.0,
) -> str:
    """POST the RPC and return the response body; raises on persistent failure."""
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            r = session.post(url, data=payload.encode("utf-8"), timeout=timeout)
            r.raise_for_status()
            body = r.text
            if body.startswith("//EX"):
                raise ValueError(f"GWT server exception: {body[:300]}")
            return body
        except Exception as e:
            last_exc = e
            wait = 2 ** attempt
            log.debug("Attempt %d/%d failed (%s) — retrying in %ds", attempt + 1, max_retries, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"All {max_retries} attempts failed") from last_exc


# ---------------------------------------------------------------------------
# Worker task
# ---------------------------------------------------------------------------

def _process_one(
    part: dict,
    *,
    template_payload: str,
    template_entity_ref: str,
    rpc_url: str,
    session: requests.Session,
    dumps_dir: Path,
    failed_path: Path,
    delay: float,
) -> Optional[Path]:
    """Fetch and dump one part's detail response.  Returns dump path or None on error."""
    display_name = part.get("display_name", "")
    entity_ref = part.get("entity_ref", display_name)
    key = display_name_key(display_name)
    dump_path = dumps_dir / f"{key}.txt"

    if dump_path.exists() and dump_path.stat().st_size > 0:
        return dump_path  # already done

    try:
        payload = _build_rpc_payload(template_payload, template_entity_ref, entity_ref)
    except ValueError as e:
        log.warning("[detail] %s — cannot build payload: %s", display_name[:60], e)
        append_failed_detail(failed_path, display_name, str(e))
        return None

    try:
        body = fetch_detail(
            session,
            rpc_url,
            payload,
            max_retries=config.DETAIL_MAX_RETRIES,
            timeout=config.DETAIL_RPC_TIMEOUT,
        )
        dump_path.write_text(body, encoding="utf-8")
        if delay > 0:
            time.sleep(delay)
        return dump_path
    except Exception as e:
        log.warning("[detail] FAILED: %s — %s", display_name[:60], e)
        append_failed_detail(failed_path, display_name, str(e))
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_detail_client(
    *,
    data_dir: Path,
    workers: int = 1,
    delay: float = 0.3,
    limit: Optional[int] = None,
    resume: bool = True,
) -> int:
    """
    Fetch getStockForStockroom for every part in parts_index.jsonl.
    Returns the count of successfully-dumped parts.
    """
    parts_index_path = data_dir / "parts_index.jsonl"
    dumps_dir = data_dir / "detail_dumps"
    rpc_template_path = data_dir / "rpc_template.json"
    auth_state_path = data_dir / "auth_state.json"
    failed_path = data_dir / "failed_details.json"

    dumps_dir.mkdir(parents=True, exist_ok=True)

    # Load template
    if not rpc_template_path.exists():
        raise FileNotFoundError(
            f"{rpc_template_path} not found. "
            "Run the enumerate phase first (collector.py will capture the template "
            "when a getStockForStockroom RPC fires). "
            "If it never fired, re-run collector with --headful and click on a part."
        )
    tpl = load_json(rpc_template_path)
    if not tpl or not tpl.get("payload"):
        raise ValueError("rpc_template.json is empty or missing 'payload' key.")

    template_payload: str = tpl["payload"]
    template_entity_ref: str = tpl.get("entity_ref") or ""
    rpc_url: str = tpl["url"]
    gwt_permutation: str = tpl.get("gwt_permutation") or ""

    if not template_entity_ref:
        raise ValueError(
            "rpc_template.json has no 'entity_ref'. "
            "Re-capture the template by running collector --headful and clicking a part."
        )

    # Load parts index
    parts = load_jsonl(parts_index_path)
    if not parts:
        log.warning("[detail] parts_index.jsonl is empty — run enumerate phase first.")
        return 0

    if limit is not None:
        parts = parts[:limit]

    # Filter already-done if resume
    if resume:
        pending = [p for p in parts if not (dumps_dir / f"{display_name_key(p.get('display_name',''))}.txt").exists()]
        skipped = len(parts) - len(pending)
        if skipped:
            log.info("[detail] Resume: skipping %d already-dumped parts (%d pending)", skipped, len(pending))
        parts = pending

    if not parts:
        log.info("[detail] Nothing to fetch — all dumps already exist.")
        return 0

    log.info("[detail] Fetching %d parts with %d worker(s)", len(parts), workers)
    session = _build_session(auth_state_path, gwt_permutation, rpc_url)

    success = 0
    failed = 0
    empty_responses = 0

    # Use workers=1 for sequential requests (avoids Promasch rate-limiting);
    # increase only when the site is tolerant.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _process_one,
                part,
                template_payload=template_payload,
                template_entity_ref=template_entity_ref,
                rpc_url=rpc_url,
                session=session,
                dumps_dir=dumps_dir,
                failed_path=failed_path,
                delay=delay,
            ): part
            for part in parts
        }
        for fut in as_completed(futures):
            part = futures[fut]
            dn = part.get("display_name", "?")[:50]
            try:
                result = fut.result()
                if result:
                    # Check if the dump is an empty ArrayList (server returned no data)
                    try:
                        body = result.read_text(encoding="utf-8")
                        import re as _re
                        if _re.match(r"//OK\[0,\d+,\[", body.strip()):
                            empty_responses += 1
                        else:
                            success += 1
                    except Exception:
                        success += 1
                    if (success + empty_responses) % 50 == 0:
                        log.info("[detail] Progress: %d real / %d empty / %d failed",
                                 success, empty_responses, failed)
                else:
                    failed += 1
            except Exception as e:
                log.warning("[detail] Unhandled error for %s: %s", dn, e)
                failed += 1

    total = success + empty_responses + failed
    log.info("[detail] Complete: %d real, %d empty-ArrayList, %d failed (of %d total)",
             success, empty_responses, failed, total)
    if empty_responses > 0 and success == 0:
        log.warning(
            "[detail] ALL %d responses were empty ArrayLists — getStockForStockroom "
            "returned no data for these parts. The parse phase will fall back to "
            "bulk_dumps automatically. To diagnose: re-capture rpc_template.json by "
            "clicking a part from the SAME category as the parts you are fetching.",
            empty_responses,
        )
    return success
