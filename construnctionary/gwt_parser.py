"""
PartTO-oriented GWT response parser: normalize, string table, stream scan, field map.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import json5

# Brand never contains '(' so anchor on the first '(' boundary.
# This correctly handles models with nested parens, e.g. HEPA Fltr Al(915(W)X610(L)).12
DISPLAY_NAME_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")

# GWT-RPC uses small negative integers as back-reference / type-descriptor
# markers in the serialization stream.  They are never real numeric field values.
_GWT_SENTINELS = frozenset(range(-30, 0))


# ---------------------------------------------------------------------------
# Step 1 – normalise raw GWT text
# ---------------------------------------------------------------------------

_CONCAT_RE = re.compile(r'\]\s*\.concat\s*\(')


def _collapse_concat_arrays(t: str) -> str:
    """Merge GWT's chunked array notation into a single array.

    When a GWT response is large, the serializer emits:
        [a, b, ...].concat([c, d, ...]).concat([e, f, ...])
    or with multiple arguments:
        [a, b, ...].concat([c, d, ...], [e, f, ...])

    json5 can't evaluate JS method calls, so we collapse all
    .concat() calls into a single flat array before parsing.

    Strategy: iteratively find each ].concat( call, parse its
    comma-separated [...] arguments (respecting bracket depth and
    quoted strings), and splice the inner content back into the
    surrounding array.
    """
    while True:
        m = _CONCAT_RE.search(t)
        if not m:
            break

        # m matches ].concat( — parse comma-separated [...] args until )
        j = m.end()
        n = len(t)
        parts: list[str] = []

        while j < n:
            while j < n and t[j] in ' \t\n\r,':
                j += 1
            if j >= n:
                break
            if t[j] == ')':
                j += 1
                break
            if t[j] == '[':
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
                    elif ch == '[':
                        depth += 1
                    elif ch == ']':
                        depth -= 1
                    j += 1
                parts.append(t[start:j - 1])
            else:
                start = j
                while j < n and t[j] not in ',)':
                    j += 1
                parts.append(t[start:j])

        # Rebuild: drop the ] before .concat( and the trailing ),
        # emit comma + unwrapped inner content + closing ]
        t = t[:m.start()] + ',' + ','.join(parts) + ']' + t[j:]

    return t


def normalize_gwt_response(text: str) -> List[Any]:
    t = text.strip()
    if t.startswith("//EX"):
        raise ValueError(f"GWT exception response: {t[:500]}")
    if t.startswith("//OK"):
        t = t[4:].strip()
    # GWT splits large responses as [a,b,...].concat([c,d,...]) — collapse first
    t = _collapse_concat_arrays(t)
    # json5 handles single-quoted strings and trailing commas common in GWT
    return json5.loads(t)


# ---------------------------------------------------------------------------
# Step 2 – split into (primitive stream, string table, tail)
# ---------------------------------------------------------------------------

def _looks_like_string_table_cell(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if s.startswith("http://") or s.startswith("https://"):
        return True
    if "/" in s and ("java." in s or "com.l3" in s):
        return True
    if len(s) > 80:
        return True
    return False


def _find_string_table_index(data: Sequence[Any]) -> Optional[int]:
    """Locate the string-table list (usually near the tail, before trailing int flags)."""
    best: Optional[Tuple[int, int]] = None
    for i, el in enumerate(data):
        if not isinstance(el, list) or len(el) < 2:
            continue
        if not all(isinstance(x, str) for x in el):
            continue
        score = sum(1 for x in el if _looks_like_string_table_cell(x))
        if score >= 2 or (len(el) >= 5 and score >= 1):
            cand = (score, i)
            if best is None or cand[0] >= best[0]:
                best = cand
    return best[1] if best else None


def split_primitive_stream_and_table(
    data: List[Any],
) -> Tuple[List[Any], List[str], List[Any]]:
    st_idx = _find_string_table_index(data)
    if st_idx is None:
        return list(data), [], []
    primitives = list(data[:st_idx])
    st = [str(x) for x in data[st_idx]]
    tail = list(data[st_idx + 1:])
    return primitives, st, tail


# ---------------------------------------------------------------------------
# Step 3 – string-table helpers
# ---------------------------------------------------------------------------

def resolve_st_value(v: Any, st: List[str]) -> Any:
    """Resolve a 1-based string-table reference."""
    if not st or not isinstance(v, int):
        return v
    if 1 <= v <= len(st):
        return st[v - 1]
    return v


def parse_display_name(name: str) -> Dict[str, str]:
    m = DISPLAY_NAME_RE.match(name.strip())
    if not m:
        return {}
    return {
        "brand": m.group(1).strip(),
        "model": m.group(2).strip(),
        "id": f"{m.group(2)}.{m.group(3)}",
    }


# ---------------------------------------------------------------------------
# Step 4 – find display-name positions in the primitive stream
# ---------------------------------------------------------------------------

def find_display_name_positions(
    primitives: Sequence[Any], st: List[str]
) -> List[Tuple[int, str]]:
    """Return (stream_index, display_name) for every PartTO name reference.

    Each display name appears exactly once in the forward stream as an integer
    ST index (1-based).  We deduplicate so the same name isn't counted twice
    even if (unlikely) it appears more than once.
    """
    seen: set = set()
    result: List[Tuple[int, str]] = []
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
# Step 5 – per-part data extraction from a stream segment
# ---------------------------------------------------------------------------

def _st_ref(v: Any, st: List[str]) -> Optional[str]:
    """Return the ST string for integer v (1-based), or None."""
    if isinstance(v, int) and 1 <= v <= len(st):
        return st[v - 1]
    return None


_VENDOR_KEYWORDS = ("pvt", "ltd", "llp", "enterprise", "limited", "solutions",
                    "industries", "trading", "suppliers", "distributors")


def _is_vendor_name(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:30]:
        return False
    sl = s.lower()
    return any(kw in sl for kw in _VENDOR_KEYWORDS)


_INDIA_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand", "karnataka",
    "kerala", "madhya pradesh", "maharashtra", "manipur", "meghalaya",
    "mizoram", "nagaland", "odisha", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "delhi", "jammu and kashmir", "ladakh",
    "dadra and nagar haveli", "daman and diu", "lakshadweep", "puducherry",
    "chandigarh", "andaman and nicobar",
}


def _is_location(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:20]:
        return False
    return s.strip().lower() in _INDIA_STATES


def extract_part_data_from_segment(
    segment: Sequence[Any], st: List[str]
) -> Dict[str, Any]:
    """Extract prices, images, vendors, location and created_by from a part segment.

    Key invariant: integers in [1, len(st)] are string-table references and must
    be checked for semantic meaning (image URL, vendor, location, person name)
    BEFORE considering them as numeric values.  Only floats and integers outside
    the ST range are treated as price candidates.
    """
    images: List[str] = []
    vendors: List[str] = []
    location: Optional[str] = None
    created_by: Optional[str] = None
    price_candidates: List[float] = []

    st_len = len(st)

    for v in segment:
        # --- integer in ST range → string-table reference (not a price) ---
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= st_len:
            ref = st[v - 1]
            if not isinstance(ref, str):
                continue
            if ref.startswith("http"):
                if ref not in images:
                    images.append(ref)
            elif _is_vendor_name(ref):
                if ref not in vendors:
                    vendors.append(ref)
            elif _is_location(ref):
                if location is None:
                    location = ref
            elif (
                not ref.startswith("com.")
                and not ref.startswith("java.")
                and "/" not in ref
                and 4 <= len(ref) <= 60
                and " " in ref
                and any(c.isupper() for c in ref)
                and not DISPLAY_NAME_RE.match(ref)
            ):
                # Heuristic: human name (mixed case, spaces, short, no slashes)
                if created_by is None:
                    created_by = ref
            continue

        # --- float or integer outside ST range → potential price ---
        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and v not in _GWT_SENTINELS
            and v > 0
        ):
            fv = float(v)
            # Ignore tiny values that are GWT type markers / list counts
            if fv > 100:
                price_candidates.append(fv)

    # With prices de-duped and sorted: the FIRST large float cluster is typically
    # (total_purchase_amount, qty, ..., market_total).  Use min as last-purchase
    # and max as market to be conservative.
    unique_prices = sorted(set(price_candidates))
    price_last_purchase: Optional[float] = None
    price_market: Optional[float] = None
    if len(unique_prices) >= 2:
        price_last_purchase = unique_prices[0]
        price_market = unique_prices[-1]
    elif len(unique_prices) == 1:
        price_last_purchase = unique_prices[0]
        price_market = unique_prices[0]

    # Scan for qty pattern: [large_price, 0.0, qty_float, ...]
    # Stream layout: total_purchase_amount, 0.0, qty (whole number), 0.0, 0.0
    purchase_qty: Optional[int] = None
    seg_list = list(segment)
    for j in range(len(seg_list) - 2):
        v0, v1, v2 = seg_list[j], seg_list[j + 1], seg_list[j + 2]
        if (
            isinstance(v0, float) and v0 > 100
            and v1 == 0.0
            and isinstance(v2, float)
            and 0 < v2 < 1000
            and v2 == int(v2)
        ):
            purchase_qty = int(v2)
            break

    return {
        "price_last_purchase": price_last_purchase if price_last_purchase else 0.0,
        "price_market": price_market if price_market else 0.0,
        "purchase_qty": purchase_qty,
        "images": images,
        "vendors": vendors,
        "location": location,
        "created_by": created_by,
    }


# ---------------------------------------------------------------------------
# Step 6 – main parse entry points
# ---------------------------------------------------------------------------

def parse_dump_text(text: str) -> Dict[str, Any]:
    data = normalize_gwt_response(text)
    if not isinstance(data, list):
        raise TypeError("Expected top-level GWT array")

    primitives, st, tail = split_primitive_stream_and_table(data)
    warnings: List[str] = []

    display_refs = find_display_name_positions(primitives, st)
    parts: List[Dict[str, Any]] = []

    if display_refs:
        n = len(display_refs)
        for idx, (pos, name) in enumerate(display_refs):
            # Segment: strictly between this display-name position and the next.
            # GWT serialises PartTO fields in order, so prices/images/vendors
            # all appear AFTER the display-name reference in the forward stream.
            # Using strict non-overlapping windows prevents bleed-through from
            # neighbouring parts.
            end = display_refs[idx + 1][0] if idx + 1 < n else len(primitives)
            segment = primitives[pos:end]

            meta = parse_display_name(name)
            extra = extract_part_data_from_segment(segment, st)

            parts.append({
                **meta,
                "display_name": name,
                "price_last_purchase": extra["price_last_purchase"],
                "price_market": extra["price_market"],
                "purchase_qty": extra["purchase_qty"],
                "vendor_count": len(extra["vendors"]) or None,
                "images": extra["images"],
                "specifications": {},
                "vendors": extra["vendors"],
                "location": extra["location"],
                "category_path": [],
                "created_by": extra["created_by"],
            })
    else:
        warnings.append("no_display_names_found")

    return {
        "parts": parts,
        "meta": {
            "string_table_len": len(st),
            "primitive_len": len(primitives),
            "display_name_count": len(display_refs),
            "tail_preview": tail[:5] if tail else [],
            "warnings": warnings,
        },
    }


def parse_dump_file(path: Path, category_path: list = []) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    result = parse_dump_text(text)
    for part in result["parts"]:
        part["category_path"] = category_path
    result["source_dump"] = str(path)
    return result


def write_parsed(path: Path, out_path: Path) -> None:
    data = parse_dump_file(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_all_dumps(
    dumps_dir: Path,
    parsed_dir: Path,
    *,
    pattern: str = "*.txt",
) -> List[Path]:
    parsed_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for f in sorted(dumps_dir.glob(pattern)):
        if not f.is_file():
            continue
        out = parsed_dir / f"{f.stem}.json"
        try:
            write_parsed(f, out)
            written.append(out)
        except Exception as e:
            log = parsed_dir.parent / "logs" / "parse_errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as lf:
                lf.write(f"{f}: {e}\n")
    return written


def aggregate_parts(parsed_files: Sequence[Path]) -> Dict[str, Any]:
    all_parts: List[Dict[str, Any]] = []
    for p in parsed_files:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            all_parts.extend(doc.get("parts", []))
        except Exception:
            continue
    return {"total": len(all_parts), "parts": all_parts}
