"""
Parse one getStockForStockroom GWT response into a canonical part record.

Design: strict-label parsing — we scan the GWT string table for known UI
labels ("Marketplace Price", "Cooling Capacity (BTU/Hr)", etc.) and read
the adjacent primitive value, so adding/removing UI fields never silently
breaks other fields.

Canonical record schema
-----------------------
{
  "display_name": str,
  "entity_ref":   str,           # same as display_name
  "entity_id":    int | str,     # int from response, or sha1-derived hex
  "entity_id_source": "response" | "hashed",
  "brand":        str | None,
  "model":        str | None,
  "category_path": [str],
  "price_market":       float | None,
  "price_last_purchase": float | None,
  "purchase_qty":       float | None,
  "stock_qty":          float | None,
  "total_value":        float | None,
  "uom":          str | None,
  "specifications": {label: value},
  "vendors":      [{"name": str, "state": str | None}],
  "marketplace_vendors": [str],
  "image_urls":   [str],
  "created_by":   str | None,
  "used_in":      [str],
  "documents":    [str],
  "image_s3_urls": [],           # filled by image_pipeline
  "_parse_warnings": [str],
}
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Known UI label → canonical field name
# ---------------------------------------------------------------------------

# Scalar numeric / string fields labeled in the detail panel.
# Keys are exact strings that appear in Promasch's GWT string table.
PRICE_LABELS: dict[str, str] = {
    "Marketplace Price":         "price_market",
    "My Last Purchase Price":    "price_last_purchase",
    "My Purchase History":       "purchase_qty",
    "Total Unit Value":          "total_value",
    "Stock":                     "stock_qty",
    "Stock Quantity":            "stock_qty",
    "Unit of Measurement":       "uom",
    "UOM":                       "uom",
    "Created By":                "created_by",
    "Added By":                  "created_by",
}

# Known spec-section labels (these appear as row headers in the spec grid).
# Any string-table entry immediately followed by another string is treated as
# a potential spec label → spec value pair when it is NOT in PRICE_LABELS and
# doesn't look like a class name or URL.
KNOWN_SPEC_LABELS_RE = re.compile(
    r"^(Model|Make|Brand|Make/Brand|Cooling Capacity|Heating Capacity|"
    r"Power Consumption|Voltage|Frequency|Refrigerant|Noise Level|"
    r"Energy Rating|Star Rating|Tonnage|Capacity|Dimensions|Weight|"
    r"Color|Colour|Warranty|Country of Origin|Material|Type|Phase|"
    r"Speed|Pressure|Flow Rate|Power|Current|Temperature|"
    r"[A-Z][A-Za-z /()°%-]+)\s*$"
)

# Display-name format: Brand(Model).revision
DISPLAY_NAME_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")

# GWT sentinel small-negative integers — never real field values
_GWT_SENTINELS = frozenset(range(-30, 0))

# Known Indian states for vendor-location heuristic
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


# ---------------------------------------------------------------------------
# GWT normalization helpers (self-contained copy)
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


def normalize_gwt_response(text: str) -> list[Any]:
    t = text.strip()
    if t.startswith("//EX"):
        raise ValueError(f"GWT exception response: {t[:300]}")
    if t.startswith("//OK"):
        t = t[4:].strip()
    t = _collapse_concat_arrays(t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        import json5  # type: ignore
        return json5.loads(t)


# ---------------------------------------------------------------------------
# String-table extraction
# ---------------------------------------------------------------------------

def _looks_like_st_cell(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if s.startswith("http://") or s.startswith("https://"):
        return True
    if "/" in s and ("java." in s or "com.l3" in s or "com.google" in s):
        return True
    if len(s) > 80:
        return True
    return False


def split_gwt(data: list[Any]) -> tuple[list[Any], list[str], list[Any]]:
    """Return (primitive_stream, string_table, tail)."""
    best_idx: Optional[int] = None
    best_score = -1
    for i, el in enumerate(data):
        if not isinstance(el, list) or len(el) < 2:
            continue
        if not all(isinstance(x, str) for x in el):
            continue
        score = sum(1 for x in el if _looks_like_st_cell(x))
        if score > best_score or (score == best_score and best_idx is not None and len(el) > len(data[best_idx])):
            best_score = score
            best_idx = i
    if best_idx is None:
        return list(data), [], []
    st = [str(x) for x in data[best_idx]]
    return list(data[:best_idx]), st, list(data[best_idx + 1 :])


def resolve(v: Any, st: list[str]) -> Any:
    """Resolve a 1-based string-table reference to its string value."""
    if isinstance(v, int) and not isinstance(v, bool) and st and 1 <= v <= len(st):
        return st[v - 1]
    return v


# ---------------------------------------------------------------------------
# entity_id extraction
# ---------------------------------------------------------------------------

def _hashed_entity_id(display_name: str, category_path: list[str]) -> str:
    key = display_name + "|" + "|".join(category_path)
    return hashlib.sha1(key.encode()).hexdigest()


def _find_entity_id(primitives: list[Any], st: list[str]) -> Optional[int]:
    """
    Heuristic: the internal PK is typically a large positive integer that
    appears in the primitive stream and is NOT a string-table reference.

    We look for integers > 1000 that are outside the string-table index range
    (i.e., > len(st)) — these cannot be ST references and are candidate IDs.
    We take the first such integer as the entity_id.
    """
    st_len = len(st)
    candidates: list[int] = []
    for v in primitives:
        if (
            isinstance(v, int)
            and not isinstance(v, bool)
            and v > st_len          # definitely not an ST reference
            and v > 1000            # filter out small type markers
            and v not in _GWT_SENTINELS
        ):
            candidates.append(v)
    if candidates:
        return candidates[0]
    return None


# ---------------------------------------------------------------------------
# Strict-label parsing
# ---------------------------------------------------------------------------

def _st_index_of(label: str, st: list[str]) -> Optional[int]:
    """Return the 0-based index in st where label occurs, or None."""
    try:
        return st.index(label)
    except ValueError:
        return None


def _read_value_after_label(primitives: list[Any], st: list[str], label: str) -> Any:
    """
    Find the ST reference for 'label' in the primitive stream, then return the
    resolved value of the next primitive.

    GWT encodes labeled fields roughly as: [st_ref_for_label, st_ref_or_value, ...].
    We take the first occurrence where the next element is either:
      - an ST reference → resolved to string
      - a float/int outside ST range → treated as the numeric value
    """
    label_idx = _st_index_of(label, st)
    if label_idx is None:
        return None
    st_ref_for_label = label_idx + 1  # GWT uses 1-based ST references
    for i, v in enumerate(primitives):
        if v == st_ref_for_label and i + 1 < len(primitives):
            nxt = primitives[i + 1]
            return resolve(nxt, st)
    return None


def extract_labeled_fields(
    primitives: list[Any], st: list[str], label_map: dict[str, str]
) -> dict[str, Any]:
    """Extract all known labeled fields into a flat dict of canonical_field → value."""
    result: dict[str, Any] = {}
    for label, field in label_map.items():
        if field in result:
            continue  # already found (e.g., both "Stock" and "Stock Quantity")
        val = _read_value_after_label(primitives, st, label)
        if val is not None:
            result[field] = val
    return result


# ---------------------------------------------------------------------------
# Spec grid parsing
# ---------------------------------------------------------------------------

def extract_specifications(st: list[str], primitives: list[Any]) -> dict[str, str]:
    """
    Scan consecutive string-table pairs where:
      - The first element looks like a spec label (matches KNOWN_SPEC_LABELS_RE)
      - The second element is a non-empty string value
      - Neither is a class name, URL, or display name

    Returns {label: value}.
    """
    specs: dict[str, str] = {}

    # Build a set of all "boring" strings to exclude
    boring_prefixes = ("com.", "java.", "http://", "https://", "//")
    price_labels_set = set(PRICE_LABELS.keys())

    st_len = len(st)
    for i in range(st_len - 1):
        label = st[i]
        value = st[i + 1]
        if not isinstance(label, str) or not isinstance(value, str):
            continue
        if not label or not value:
            continue
        if any(label.startswith(p) for p in boring_prefixes):
            continue
        if any(value.startswith(p) for p in boring_prefixes):
            continue
        if label in price_labels_set:
            continue
        if DISPLAY_NAME_RE.match(label.strip()):
            continue
        if not KNOWN_SPEC_LABELS_RE.match(label.strip()):
            continue
        if label not in specs:
            specs[label] = value

    return specs


# ---------------------------------------------------------------------------
# Vendor extraction
# ---------------------------------------------------------------------------

def _is_vendor_name(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:30]:
        return False
    sl = s.lower()
    return any(kw in sl for kw in _VENDOR_KEYWORDS)


def _is_state(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:20]:
        return False
    return s.strip().lower() in _INDIA_STATES


def extract_vendors(st: list[str]) -> list[dict[str, Optional[str]]]:
    """
    Find (vendor_name, state?) pairs in the string table.
    A vendor is followed immediately by its state when the state entry is present.
    """
    vendors: list[dict[str, Optional[str]]] = []
    seen: set[str] = set()
    st_len = len(st)
    i = 0
    while i < st_len:
        s = st[i]
        if _is_vendor_name(s) and s not in seen:
            seen.add(s)
            state: Optional[str] = None
            if i + 1 < st_len and _is_state(st[i + 1]):
                state = st[i + 1]
                i += 1
            vendors.append({"name": s, "state": state})
        i += 1
    return vendors


# ---------------------------------------------------------------------------
# Image URL extraction
# ---------------------------------------------------------------------------

_IMAGE_URL_RE = re.compile(r"https?://\S+/(?:getPartImage|image|img|photo)\S*", re.I)


def extract_image_urls(st: list[str]) -> list[str]:
    """Return all Promasch image URLs found in the string table."""
    urls: list[str] = []
    seen: set[str] = set()
    for s in st:
        if not isinstance(s, str):
            continue
        if not s.startswith("http"):
            continue
        # Accept any URL that looks like an image endpoint (adjust regex as needed)
        if (
            _IMAGE_URL_RE.search(s)
            or s.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
            or "image" in s.lower()
            or "photo" in s.lower()
        ):
            if s not in seen:
                seen.add(s)
                urls.append(s)
    return urls


# ---------------------------------------------------------------------------
# Main parse entry point
# ---------------------------------------------------------------------------

def parse_detail_response(
    text: str,
    *,
    display_name: str = "",
    entity_ref: str = "",
    category_path: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    Parse a raw getStockForStockroom GWT response and return a canonical record.

    Parameters
    ----------
    text            Raw response body (starts with //OK or //EX).
    display_name    From parts_index.jsonl — used for fallback entity_id.
    entity_ref      From parts_index.jsonl — the string key passed to the RPC.
    category_path   From parts_index.jsonl.
    """
    warnings: list[str] = []
    category_path = category_path or []

    try:
        data = normalize_gwt_response(text)
    except Exception as e:
        return _error_record(display_name, entity_ref, category_path, f"normalize failed: {e}")

    if not isinstance(data, list):
        return _error_record(display_name, entity_ref, category_path, "top-level not a list")

    primitives, st, _ = split_gwt(data)

    # ── entity_id ──────────────────────────────────────────────────────────
    entity_id_int = _find_entity_id(primitives, st)
    if entity_id_int is not None:
        entity_id: Any = entity_id_int
        entity_id_source = "response"
    else:
        entity_id = _hashed_entity_id(display_name or entity_ref, category_path)
        entity_id_source = "hashed"
        warnings.append("entity_id_not_found_in_stream")

    # ── display_name / brand / model ───────────────────────────────────────
    dn = display_name or entity_ref
    m = DISPLAY_NAME_RE.match(dn.strip()) if dn else None
    brand = m.group(1).strip() if m else None
    model = m.group(2).strip() if m else None

    # ── Labeled scalar fields ──────────────────────────────────────────────
    labeled = extract_labeled_fields(primitives, st, PRICE_LABELS)

    def _to_float(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _to_str(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    price_market = _to_float(labeled.get("price_market"))
    price_last_purchase = _to_float(labeled.get("price_last_purchase"))
    purchase_qty = _to_float(labeled.get("purchase_qty"))
    stock_qty = _to_float(labeled.get("stock_qty"))
    total_value = _to_float(labeled.get("total_value"))
    uom = _to_str(labeled.get("uom"))
    created_by = _to_str(labeled.get("created_by"))

    if not any([price_market, price_last_purchase, purchase_qty]):
        warnings.append("no_labeled_prices_found")

    # ── Specifications ─────────────────────────────────────────────────────
    specifications = extract_specifications(st, primitives)

    # ── Vendors ────────────────────────────────────────────────────────────
    vendors = extract_vendors(st)

    # ── Marketplace vendors (raw list of vendor names without state context) ─
    marketplace_vendors: list[str] = [v["name"] for v in vendors]

    # ── Image URLs ─────────────────────────────────────────────────────────
    image_urls = extract_image_urls(st)

    return {
        "display_name": dn,
        "entity_ref": entity_ref or dn,
        "entity_id": entity_id,
        "entity_id_source": entity_id_source,
        "brand": brand,
        "model": model,
        "category_path": category_path,
        "price_market": price_market,
        "price_last_purchase": price_last_purchase,
        "purchase_qty": purchase_qty,
        "stock_qty": stock_qty,
        "total_value": total_value,
        "uom": uom,
        "specifications": specifications,
        "vendors": vendors,
        "marketplace_vendors": marketplace_vendors,
        "image_urls": image_urls,
        "created_by": created_by,
        "used_in": [],
        "documents": [],
        "image_s3_urls": [],
        "_parse_warnings": warnings,
    }


def _error_record(
    display_name: str, entity_ref: str, category_path: list[str], error: str
) -> dict[str, Any]:
    dn = display_name or entity_ref
    return {
        "display_name": dn,
        "entity_ref": entity_ref or dn,
        "entity_id": _hashed_entity_id(dn, category_path),
        "entity_id_source": "hashed",
        "brand": None,
        "model": None,
        "category_path": category_path,
        "price_market": None,
        "price_last_purchase": None,
        "purchase_qty": None,
        "stock_qty": None,
        "total_value": None,
        "uom": None,
        "specifications": {},
        "vendors": [],
        "marketplace_vendors": [],
        "image_urls": [],
        "created_by": None,
        "used_in": [],
        "documents": [],
        "image_s3_urls": [],
        "_parse_warnings": [f"parse_error: {error}"],
    }


# ---------------------------------------------------------------------------
# Batch parse: detail_dumps/ → parts_records.jsonl
# ---------------------------------------------------------------------------

def parse_dump_file(
    dump_path: Path,
    *,
    display_name: str = "",
    entity_ref: str = "",
    category_path: Optional[list[str]] = None,
) -> dict[str, Any]:
    text = dump_path.read_text(encoding="utf-8")
    return parse_detail_response(
        text,
        display_name=display_name,
        entity_ref=entity_ref,
        category_path=category_path,
    )


def run_parse(
    *,
    data_dir: Path,
    limit: Optional[int] = None,
    resume: bool = True,
) -> int:
    """
    Parse all detail dumps → write/extend parts_records.jsonl.

    Primary source: detail_dumps/{key}.txt  (getStockForStockroom responses).
    Fallback source: bulk_dumps/{source_dump}  (getPartDetails bulk responses)
    used when the detail dump is absent or is an empty-ArrayList response.

    Returns count of records written.
    """
    from bulk_parser import is_empty_gwt_response, parse_bulk_dump
    from utils import append_jsonl, display_name_key, load_jsonl, setup_logging

    log = setup_logging("detail_parser")

    parts_index_path = data_dir / "parts_index.jsonl"
    detail_dumps_dir = data_dir / "detail_dumps"
    bulk_dumps_dir = data_dir / "bulk_dumps"
    records_path = data_dir / "parts_records.jsonl"

    index = load_jsonl(parts_index_path)
    if not index:
        raise FileNotFoundError(f"parts_index.jsonl not found or empty in {data_dir}")

    # Build set of already-parsed entity_refs for resume
    existing_refs: set[str] = set()
    if resume and records_path.exists():
        for rec in load_jsonl(records_path):
            ref = rec.get("entity_ref") or rec.get("display_name", "")
            if ref:
                existing_refs.add(ref)

    # Pre-parse bulk dumps lazily (keyed by resolved path string)
    _bulk_cache: dict[str, dict[str, dict]] = {}

    def _get_bulk_data(source_dump: str) -> dict[str, dict]:
        """Return parsed bulk data for a source_dump path (cached)."""
        if source_dump not in _bulk_cache:
            # source_dump is relative to data_dir in parts_index
            candidate = data_dir / source_dump
            if not candidate.exists():
                candidate = bulk_dumps_dir / Path(source_dump).name
            if candidate.exists():
                _bulk_cache[source_dump] = parse_bulk_dump(candidate)
            else:
                _bulk_cache[source_dump] = {}
        return _bulk_cache[source_dump]

    written = 0
    skipped = 0
    bulk_used = 0

    for entry in (index[:limit] if limit else index):
        dn = entry.get("display_name", "")
        entity_ref = entry.get("entity_ref", dn)
        category_path = entry.get("category_path", [])
        source_dump = entry.get("source_dump", "")

        if resume and entity_ref in existing_refs:
            skipped += 1
            continue

        key = display_name_key(dn)
        detail_path = detail_dumps_dir / f"{key}.txt"

        # --- Try detail dump first ---
        use_detail = False
        if detail_path.exists() and detail_path.stat().st_size > 0:
            raw = detail_path.read_text(encoding="utf-8")
            if not is_empty_gwt_response(raw):
                use_detail = True

        if use_detail:
            try:
                record = parse_dump_file(
                    detail_path,
                    display_name=dn,
                    entity_ref=entity_ref,
                    category_path=category_path,
                )
                append_jsonl(records_path, record)
                written += 1
                if written % 100 == 0:
                    log.info("[parse] %d records written (%d skipped)", written, skipped)
                continue
            except Exception as e:
                log.warning("[parse] detail_dump parse failed for %s: %s — falling back to bulk", dn[:60], e)

        # --- Fallback: parse from bulk dump ---
        if not source_dump:
            log.debug("[parse] No source_dump for %s — skipping", dn[:60])
            continue

        bulk_data = _get_bulk_data(source_dump)
        part_data = bulk_data.get(dn)
        if part_data is None:
            log.debug("[parse] %s not found in bulk dump %s", dn[:60], source_dump)
            continue

        m = DISPLAY_NAME_RE.match(dn.strip()) if dn else None
        brand = m.group(1).strip() if m else None
        model = m.group(2).strip() if m else None

        record = {
            "display_name": dn,
            "entity_ref": entity_ref or dn,
            "entity_id": _hashed_entity_id(dn, category_path),
            "entity_id_source": "hashed",
            "brand": brand,
            "model": model,
            "category_path": category_path,
            "price_market": part_data.get("price_market"),
            "price_last_purchase": part_data.get("price_last_purchase"),
            "purchase_qty": part_data.get("purchase_qty"),
            "stock_qty": None,
            "total_value": None,
            "uom": None,
            "specifications": {},
            "vendors": part_data.get("vendors", []),
            "marketplace_vendors": part_data.get("marketplace_vendors", []),
            "image_urls": part_data.get("image_urls", []),
            "created_by": part_data.get("created_by"),
            "used_in": [],
            "documents": part_data.get("documents", []),
            "image_s3_urls": [],
            "_parse_warnings": ["parsed_from_bulk_dump"],
        }
        append_jsonl(records_path, record)
        written += 1
        bulk_used += 1
        if written % 100 == 0:
            log.info("[parse] %d records written (%d skipped, %d from bulk)", written, skipped, bulk_used)

    if bulk_used:
        log.info(
            "[parse] Note: %d/%d records parsed from bulk dumps (getStockForStockroom "
            "returned empty data — specs not available from this source).",
            bulk_used, written,
        )
    log.info("[parse] Done: %d written, %d skipped.", written, skipped)
    return written
