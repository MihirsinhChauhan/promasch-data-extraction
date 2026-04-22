"""
Parse a getPartDetails bulk GWT response into a per-display-name data map.

getPartDetails returns data for all parts in a category in a single RPC call.
The primitive stream is segmented by display-name positions; each segment
between consecutive display names belongs to that part.

Extracted fields per part:
  price_last_purchase  float | None
  price_market         float | None
  purchase_qty         float | None
  vendors              [{"name": str, "state": str | None}]
  marketplace_vendors  [str]
  image_urls           [str]  — base S3 URLs (no signed params)
  documents            [str]  — PDF/document URLs
  created_by           str | None
  location             str | None

Specifications are NOT available in getPartDetails responses.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from detail_parser import normalize_gwt_response, split_gwt

# ---------------------------------------------------------------------------
# Display-name regex (same as the rest of the pipeline)
# ---------------------------------------------------------------------------
DISPLAY_NAME_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")

# ---------------------------------------------------------------------------
# Known Indian states (for vendor location heuristic)
# ---------------------------------------------------------------------------
_INDIA_STATES = frozenset({
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "delhi", "jammu and kashmir", "ladakh",
    "dadra and nagar haveli", "daman and diu", "lakshadweep", "puducherry",
    "chandigarh", "andaman and nicobar",
})

_VENDOR_KEYWORDS = (
    "pvt", "ltd", "llp", "enterprise", "limited", "solutions",
    "industries", "trading", "suppliers", "distributors", "corporation",
    "co.", "company", "agencies", "works",
)

# GWT sentinel values — never real field values
_GWT_SENTINELS = frozenset(range(-30, 0))

# Image / document URL patterns
_S3_BASE_RE = re.compile(r"(https?://[^?#]+)", re.I)
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".JPG", ".JPEG", ".PNG"})
_DOC_EXTS = frozenset({".pdf", ".PDF"})


# ---------------------------------------------------------------------------
# String-table helpers
# ---------------------------------------------------------------------------

def _is_vendor(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:30]:
        return False
    sl = s.lower()
    return any(kw in sl for kw in _VENDOR_KEYWORDS)


def _is_state(s: str) -> bool:
    return isinstance(s, str) and s.strip().lower() in _INDIA_STATES


def _is_human_name(s: str) -> bool:
    """Heuristic: short mixed-case string with a space that isn't a vendor or state."""
    if not isinstance(s, str):
        return False
    if s.startswith(("http", "com.", "java.")) or "/" in s:
        return False
    if not (4 <= len(s) <= 50):
        return False
    if " " not in s:
        return False
    if not any(c.isupper() for c in s):
        return False
    if DISPLAY_NAME_RE.match(s.strip()):
        return False
    # Exclude state names and vendor-like strings
    if _is_state(s):
        return False
    if _is_vendor(s):
        return False
    # Exclude strings that look like company/org names (contain digits or &)
    if "&" in s or any(c.isdigit() for c in s):
        return False
    # Must look like a personal name: 2-3 words max, each word starts with capital
    words = s.strip().split()
    if len(words) > 4:
        return False
    if not all(w[0].isupper() for w in words if w):
        return False
    return True


def _strip_signed_params(url: str) -> str:
    """Return the URL without AWS pre-signed query parameters."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment=""))


# ---------------------------------------------------------------------------
# Display-name position finder
# ---------------------------------------------------------------------------

def _find_display_name_positions(
    primitives: list[Any], st: list[str]
) -> list[tuple[int, str]]:
    """Return [(stream_index, display_name)] for every PartTO name reference."""
    seen: set[str] = set()
    result: list[tuple[int, str]] = []
    for i, v in enumerate(primitives):
        if not isinstance(v, int) or not (1 <= v <= len(st)):
            continue
        candidate = st[v - 1]
        if (
            isinstance(candidate, str)
            and candidate not in seen
            and DISPLAY_NAME_RE.match(candidate.strip())
        ):
            seen.add(candidate)
            result.append((i, candidate.strip()))
    return result


# ---------------------------------------------------------------------------
# Per-part data extraction from a stream segment
# ---------------------------------------------------------------------------

def _extract_segment(
    segment: list[Any], st: list[str]
) -> dict[str, Any]:
    """Extract prices, vendors, images, docs, location, created_by."""
    images: list[str] = []
    documents: list[str] = []
    vendors: list[str] = []
    seen_vendors: set[str] = set()
    location: Optional[str] = None
    created_by: Optional[str] = None
    price_candidates: list[float] = []

    st_len = len(st)

    for v in segment:
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= st_len:
            ref = st[v - 1]
            if not isinstance(ref, str):
                continue
            if ref.startswith("http"):
                # Skip Google Docs viewer wrapper URLs
                if "docs.google.com/gview" in ref:
                    continue
                base = _strip_signed_params(ref)
                path_lower = urlparse(base).path.lower()
                ext = "." + path_lower.rsplit(".", 1)[-1] if "." in path_lower else ""
                if ext in _IMAGE_EXTS or "imagevideo" in path_lower.replace("and", "").replace("&", ""):
                    if base not in images:
                        images.append(base)
                elif ext in _DOC_EXTS or "document" in path_lower:
                    if base not in documents:
                        documents.append(base)
                else:
                    # Could be either — skip ambiguous URLs without a clear extension
                    pass
            elif _is_vendor(ref) and ref not in seen_vendors:
                seen_vendors.add(ref)
                vendors.append(ref)
            elif _is_state(ref) and location is None:
                location = ref
            elif _is_human_name(ref) and created_by is None:
                created_by = ref
            continue

        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and v not in _GWT_SENTINELS
            and v > 100
        ):
            price_candidates.append(float(v))

    # Heuristic: sort unique prices, assign min → last_purchase, max → market.
    # Cap at 10M to filter obviously-wrong bleed-through values from adjacent parts.
    _PRICE_CAP = 10_000_000.0
    unique_prices = sorted(p for p in set(price_candidates) if p <= _PRICE_CAP)
    price_last_purchase: Optional[float] = None
    price_market: Optional[float] = None
    if len(unique_prices) >= 2:
        price_last_purchase = unique_prices[0]
        price_market = unique_prices[-1]
    elif len(unique_prices) == 1:
        price_last_purchase = unique_prices[0]
        price_market = unique_prices[0]

    # Heuristic for purchase_qty: [large_price, 0.0, qty_float, ...]
    purchase_qty: Optional[float] = None
    seg = list(segment)
    for j in range(len(seg) - 2):
        v0, v1, v2 = seg[j], seg[j + 1], seg[j + 2]
        if (
            isinstance(v0, float) and v0 > 100
            and v1 == 0.0
            and isinstance(v2, float)
            and 0 < v2 < 10000
            and v2 == int(v2)
        ):
            purchase_qty = float(v2)
            break

    return {
        "price_last_purchase": price_last_purchase,
        "price_market": price_market,
        "purchase_qty": purchase_qty,
        "vendors": [{"name": n, "state": None} for n in vendors],
        "marketplace_vendors": vendors,
        "image_urls": images,
        "documents": documents,
        "created_by": created_by,
        "location": location,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bulk_dump(path: Path) -> dict[str, dict[str, Any]]:
    """
    Parse a getPartDetails bulk GWT response.

    Returns a dict keyed by display_name → extracted part data.
    If the response is empty or unparseable, returns an empty dict.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = normalize_gwt_response(text)
    except Exception:
        return {}

    if not isinstance(data, list):
        return {}

    primitives, st, _ = split_gwt(data)
    if not st:
        return {}

    positions = _find_display_name_positions(primitives, st)
    if not positions:
        return {}

    result: dict[str, dict[str, Any]] = {}
    n = len(positions)
    for idx, (pos, display_name) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < n else len(primitives)
        segment = primitives[pos:end]
        result[display_name] = _extract_segment(segment, st)

    return result


def is_empty_gwt_response(text: str) -> bool:
    """Return True if the GWT response is an empty ArrayList (no useful data)."""
    stripped = text.strip()
    # Pattern: //OK[0,1,["java.util.ArrayList/..."],0,7]
    return bool(re.match(r"//OK\[0,\d+,\[", stripped))
