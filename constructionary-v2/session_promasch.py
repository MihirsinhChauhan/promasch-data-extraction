"""Authenticated requests session for Promasch (cookies from Playwright auth_state)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import requests


def load_cookie_jar_from_storage_state(path: Path) -> requests.cookies.RequestsCookieJar:
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


def build_image_session(auth_state_path: Optional[Path]) -> requests.Session:
    """Session for GET of image URLs; browser cookies required for gated assets."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; constructionary-v2/1.0; +https://gw.promasch.in)"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
    )
    if auth_state_path and auth_state_path.is_file():
        s.cookies.update(load_cookie_jar_from_storage_state(auth_state_path))
    return s


def fetch_url_bytes(
    session: requests.Session,
    url: str,
    *,
    timeout: float = 60.0,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[Optional[bytes], Optional[str]]:
    try:
        r = session.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                total += len(chunk)
                if total > max_bytes:
                    return None, f"response too large (>{max_bytes} bytes)"
                chunks.append(chunk)
        return b"".join(chunks), None
    except Exception as e:
        return None, str(e)
