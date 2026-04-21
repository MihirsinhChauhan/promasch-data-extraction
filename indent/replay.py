"""
Replay captured GWT POST payloads for both /erp (getIndentListCompleted)
and /erp2 (GetIndentPartsForProjectIndent) with session cookies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

DEFAULT_ERP_URL = "https://gw.promasch.in/deptherp/erp"
DEFAULT_ERP2_URL = "https://gw.promasch.in/deptherp/erp2"

GWT_HEADERS = {
    "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
    "Accept": "*/*",
}

_DETAIL_METHOD = "GetIndentPartsForProjectIndent"


def detect_endpoint_from_payload(payload_text: str) -> str:
    """Return the correct ERP endpoint URL for this payload."""
    if "ERPService2" in payload_text or _DETAIL_METHOD in payload_text:
        return DEFAULT_ERP2_URL
    return DEFAULT_ERP_URL


def _extract_gwt_headers(payload_text: str) -> Dict[str, str]:
    """Extract Referer, Origin, and X-GWT-Permutation from GWT-RPC v7 payload.

    GWT v7 wire format: 7|flags|N|s0|s1|...|sN-1|stream
      s0 = moduleBaseURL, s1 = strongName (permutation hash)
    """
    parts = payload_text.split("|")
    try:
        module_base = parts[3]
        strong_name = parts[4]
        parsed = urlparse(module_base)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return {
            "Referer": module_base,
            "Origin": origin,
            "X-GWT-Permutation": strong_name,
        }
    except (IndexError, ValueError):
        return {}


def load_cookie_jar_from_storage_state(
    path: Path,
) -> requests.cookies.RequestsCookieJar:
    raw = json.loads(path.read_text(encoding="utf-8"))
    jar = requests.cookies.RequestsCookieJar()
    for c in raw.get("cookies", []):
        jar.set(
            c["name"],
            c["value"],
            domain=c.get("domain"),
            path=c.get("path", "/"),
        )
    return jar


def build_session(
    auth_state_path: Optional[Path],
    extra_headers: Optional[Dict[str, str]] = None,
) -> requests.Session:
    s = requests.Session()
    s.headers.update(GWT_HEADERS)
    if extra_headers:
        s.headers.update(extra_headers)
    if auth_state_path and auth_state_path.is_file():
        s.cookies.update(load_cookie_jar_from_storage_state(auth_state_path))
    return s


def replay_one(
    payload_text: str,
    *,
    session: requests.Session,
    url: str,
    timeout: float = 120.0,
) -> Tuple[int, str]:
    r = session.post(url, data=payload_text.encode("utf-8"), timeout=timeout)
    return r.status_code, r.text


def replay_payload_file(
    payload_path: Path,
    output_dump_path: Path,
    *,
    auth_state_path: Optional[Path],
    erp_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Replay one payload file, auto-detecting endpoint if erp_url is not given."""
    payload_text = payload_path.read_text(encoding="utf-8")
    target_url = erp_url or detect_endpoint_from_payload(payload_text)
    gwt_headers = _extract_gwt_headers(payload_text)
    session = build_session(auth_state_path, extra_headers=gwt_headers)
    status, body = replay_one(payload_text, session=session, url=target_url)
    ok = status == 200 and body.strip().startswith("//OK")
    output_dump_path.parent.mkdir(parents=True, exist_ok=True)
    if ok:
        output_dump_path.write_text(body, encoding="utf-8")
    return {
        "payload": str(payload_path),
        "dump": str(output_dump_path),
        "status": status,
        "ok": ok,
        "skipped_write": not ok,
        "endpoint": target_url,
    }


def replay_catalog(
    data_dir: Path,
    *,
    catalog_name: str = "payload_catalog.json",
) -> List[Dict[str, Any]]:
    """Re-POST every payload in payload_catalog.json, writing fresh dumps.

    The endpoint is auto-detected per entry (erp vs erp2) from the catalog
    url field or the payload content.
    """
    catalog_path = data_dir / catalog_name
    if not catalog_path.is_file():
        raise FileNotFoundError(
            f"Missing {catalog_path}; run collector first"
        )
    entries: List[Dict[str, Any]] = json.loads(
        catalog_path.read_text(encoding="utf-8")
    )
    auth_state = data_dir / "auth_state.json"
    auth_path = auth_state if auth_state.is_file() else None
    results: List[Dict[str, Any]] = []

    for entry in entries:
        rel_payload = entry.get("payload")
        rel_dump = entry.get("dump")
        if not rel_payload or not rel_dump:
            continue
        payload_path = data_dir / rel_payload
        dump_path = data_dir / rel_dump
        # Prefer url from catalog; fall back to auto-detection
        url = entry.get("url") or None

        info = replay_payload_file(
            payload_path,
            dump_path,
            auth_state_path=auth_path,
            erp_url=url,
        )
        info["ts"] = int(time.time() * 1000)
        results.append(info)
        if info["status"] == 401:
            print(
                f"[replay] 401 for {payload_path.name} — "
                "re-run collector to refresh auth"
            )
        elif not info["ok"]:
            print(
                f"[replay] FAIL status={info['status']} → {payload_path.name}"
            )

    summary_path = data_dir / "logs" / "replay_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    ok_count = sum(1 for r in results if r.get("ok"))
    print(f"[replay] {ok_count}/{len(results)} succeeded.")
    return results
