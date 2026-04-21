"""
GWT response parser for Promasch Indent Completed data.

Handles two response types:
  1. getIndentListCompleted (/erp)   → IndentTO list (summary per indent)
  2. GetIndentPartsForProjectIndent  → IndentPartTO list (parts per indent)

Core strategy for detail responses
────────────────────────────────────
Each IndentPartTO object contains a CircuitPartTO whose circuit-info field is
a `#~#`-delimited string encoding: system, circuit part name, supply type,
flags, quantities, dates.  These strings are unique per part and act as reliable
anchors in the GWT primitive stream.

  1. Normalise GWT text (strip //OK, collapse .concat() arrays, json5 parse).
  2. Split into primitive stream + string table.
  3. Find every #~# circuit-info string in the string table.
  4. Find where each is referenced in the stream → block boundaries.
  5. For each segment between boundaries: extract ST-referenced strings
     (category, UOM, vendor, PO, status, images, dates, progress refs)
     and numeric floats (amount, GST, total).

Circuit-info parsing
────────────────────
  Format: system#~#circuit_part#~#supply_type#~#f1#~#f2#~#indent_qty
          #~#approved_qty#~#dispatched_qty#~#remaining_qty#~#f3#~#remarks
          #~#indent_date#~#required_date
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import json5

# ── GWT sentinels ─────────────────────────────────────────────────────────────

# Small negative integers used as type-descriptor markers in the GWT stream.
_GWT_SENTINELS = frozenset(range(-30, 0))

# ── GWT response normalisation (shared with construnctionary) ─────────────────

_CONCAT_RE = re.compile(r'\]\s*\.concat\s*\(')


def _collapse_concat_arrays(t: str) -> str:
    """Merge GWT's chunked .concat([...]) array notation into a single array."""
    while True:
        m = _CONCAT_RE.search(t)
        if not m:
            break
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
                parts.append(t[start: j - 1])
            else:
                start = j
                while j < n and t[j] not in ',)':
                    j += 1
                parts.append(t[start:j])
        t = t[:m.start()] + ',' + ','.join(parts) + ']' + t[j:]
    return t


def normalize_gwt_response(text: str) -> List[Any]:
    t = text.strip()
    if t.startswith("//EX"):
        raise ValueError(f"GWT exception response: {t[:500]}")
    if t.startswith("//OK"):
        t = t[4:].strip()
    t = _collapse_concat_arrays(t)
    return json5.loads(t)


# ── String table split ────────────────────────────────────────────────────────

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


# ── Circuit-info string parsing ───────────────────────────────────────────────

# The #~# fields are: system, circuit_part, supply_type, f1, f2,
# indent_qty, approved_qty, dispatched_qty, remaining_qty, f3,
# remarks, indent_date, required_date
_CIRCUIT_FIELDS = (
    "circuit_system",
    "circuit_part_name",
    "supply_type",
    "_flag1",
    "_flag2",
    "indent_qty",
    "approved_qty",
    "dispatched_qty",
    "remaining_qty",
    "_flag3",
    "remarks",
    "indent_date",
    "required_date",
)


def parse_circuit_info(raw: str) -> Dict[str, Any]:
    """Parse a #~# delimited circuit-info string into a structured dict."""
    parts = raw.split("#~#")
    result: Dict[str, Any] = {}
    for i, field in enumerate(_CIRCUIT_FIELDS):
        if field.startswith("_"):
            continue
        val = parts[i].strip() if i < len(parts) else ""
        # Convert numeric strings for quantity fields
        if field.endswith("_qty") and val:
            try:
                result[field] = float(val)
            except ValueError:
                result[field] = val
        else:
            result[field] = val or None
    return result


def _is_circuit_info(s: str) -> bool:
    return isinstance(s, str) and "#~#" in s and s.count("#~#") >= 4


# ── Semantic classifiers ──────────────────────────────────────────────────────

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

_SUPPLY_TYPES = {"SUPPLY_INSTALLATION", "SUPPLY_ONLY", "INSTALLATION_ONLY"}

_STATUS_VALUES = {
    "TRMN TKIN", "STAGE1", "STAGE2", "STAGE3", "STAGE4",
    "PARTIAL", "COMPLETED", "PENDING", "PROJECT",
}

_KNOWN_UOMS = {"NOS", "SQM", "MT", "KG", "RMT", "LM", "SET", "LS", "LOT", "MTR"}

# Part-category patterns: end with (Nos), (Sqm), (Mtr), (Kg), etc.
_CATEGORY_RE = re.compile(
    r'\((?:Nos|Sqm|Mtr|Kg|Rmt|Lm|Set|Ls|No|Mtr\.|KG|SQM|MTR|NOS)\)',
    re.IGNORECASE,
)

_VENDOR_KEYWORDS = (
    "pvt", "ltd", "llp", "enterprise", "limited", "solutions",
    "industries", "trading", "suppliers", "distributors", "corporation",
    "co.", "house", "craft", "metals", "steel", "agency", "brothers",
)

# Progress/bill reference patterns
_PROGRESS_RE = re.compile(r'^\d+#~#')

# PO reference pattern
_PO_RE = re.compile(r'^(?:PO|B\(PO)\(')

# Date patterns in string values
_DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}')

# J-indent reference pattern: J-XX-XX-Ind-NN-date-seq
_JINDENT_RE = re.compile(r'^J-[A-Z0-9]{1,6}-[A-Z0-9]{1,6}-Ind-\d+(?:-|$)')


def _is_vendor(s: str) -> bool:
    if not isinstance(s, str) or s.startswith("http") or "/" in s[:30]:
        return False
    sl = s.lower()
    return any(kw in sl for kw in _VENDOR_KEYWORDS)


def _is_location(s: str) -> bool:
    return isinstance(s, str) and s.strip().lower() in _INDIA_STATES


def _is_part_category(s: str) -> bool:
    return bool(_CATEGORY_RE.search(s))


def _is_person_name(s: str) -> bool:
    """Heuristic: human first/last name (mixed case, 2 words, no digits/parens)."""
    if not isinstance(s, str):
        return False
    stripped = s.strip()
    # Basic length and space check
    if not (4 <= len(stripped) <= 50 and " " in stripped):
        return False
    # Exclude technical/structural strings
    if "/" in stripped or "." in stripped:
        return False
    if any(c.isdigit() for c in stripped):
        return False
    if "(" in stripped or ")" in stripped:
        return False
    if stripped.startswith("com.") or stripped.startswith("java."):
        return False
    if _is_circuit_info(stripped):
        return False
    if _PO_RE.match(stripped) or _PROGRESS_RE.match(stripped):
        return False
    if _DATE_RE.match(stripped) or _JINDENT_RE.match(stripped):
        return False
    if _is_part_category(stripped):
        return False
    if stripped in _SUPPLY_TYPES or stripped in _STATUS_VALUES or stripped in _KNOWN_UOMS:
        return False
    # Exclude domain-specific multi-word terms (HVAC/MEP system names, material types)
    _DOMAIN_WORDS = {
        "system", "work", "material", "installation", "supply", "project",
        "chilled", "water", "ducting", "electrical", "piping", "hvac",
        "plumbing", "fire", "duct", "cable", "tray", "conduit",
    }
    words = stripped.lower().split()
    if any(w in _DOMAIN_WORDS for w in words):
        return False
    # Allow only 2 words (first + last name), not 3+ (system names)
    if len(words) != 2:
        return False
    # Must have at least one uppercase letter
    return any(c.isupper() for c in stripped)


# ── Find circuit-info anchor positions ───────────────────────────────────────

def find_circuit_info_positions(
    primitives: Sequence[Any],
    st: List[str],
) -> List[Tuple[int, str]]:
    """Return (stream_index, circuit_info_string) for each #~# anchor.

    Only the first occurrence of each circuit-info string is returned so that
    each part appears exactly once even if the string is referenced multiple
    times in the stream.
    """
    # Build 1-based index set of circuit-info strings in ST
    circuit_st_indices = {
        i + 1 for i, s in enumerate(st) if _is_circuit_info(s)
    }

    seen: set = set()
    result: List[Tuple[int, str]] = []
    for pos, v in enumerate(primitives):
        if isinstance(v, int) and v in circuit_st_indices and v not in seen:
            seen.add(v)
            result.append((pos, st[v - 1]))
    return result


# ── Per-part data extraction ──────────────────────────────────────────────────

def extract_part_data_from_segment(
    segment: Sequence[Any],
    st: List[str],
) -> Dict[str, Any]:
    """
    Scan a stream segment (between two circuit-info anchors) for all
    semantically meaningful values for one IndentPartTO.

    String-table references (1-based integers in [1, len(st)]) are resolved
    and classified.  Floats outside sentinel range are collected as amounts.
    """
    part_categories: List[str] = []
    uom: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_location: Optional[str] = None
    po_references: List[str] = []
    progress_refs: List[str] = []
    status: Optional[str] = None
    images: List[str] = []
    person_names: List[str] = []
    dates: List[str] = []
    j_indent_refs: List[str] = []
    raw_amounts: List[float] = []
    supply_type: Optional[str] = None

    st_len = len(st)

    for v in segment:
        # ── String-table reference ─────────────────────────────────────────
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= st_len:
            s = st[v - 1]
            if not isinstance(s, str):
                continue
            if s.startswith("http"):
                if s not in images:
                    images.append(s)
            elif _PROGRESS_RE.match(s):
                if s not in progress_refs:
                    progress_refs.append(s)
            elif _PO_RE.match(s):
                if s not in po_references:
                    po_references.append(s)
            elif _JINDENT_RE.match(s):
                if s not in j_indent_refs:
                    j_indent_refs.append(s)
            elif s in _SUPPLY_TYPES:
                if supply_type is None:
                    supply_type = s
            elif s in _STATUS_VALUES:
                if status is None:
                    status = s
            elif s in _KNOWN_UOMS:
                if uom is None:
                    uom = s
            elif _is_part_category(s):
                if s not in part_categories:
                    part_categories.append(s)
            elif _is_vendor(s):
                if vendor_name is None:
                    vendor_name = s
            elif _is_location(s):
                if vendor_location is None:
                    vendor_location = s
            elif _DATE_RE.match(s):
                if s not in dates:
                    dates.append(s)
            elif _is_person_name(s):
                if s not in person_names:
                    person_names.append(s)
            continue

        # ── Numeric value → potential amount ─────────────────────────────
        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and v not in _GWT_SENTINELS
            and v > 0
        ):
            fv = float(v)
            if fv > 1.0 and v not in range(1, st_len + 1):
                raw_amounts.append(fv)

    # ── Derive price fields ───────────────────────────────────────────────────
    # Pattern: [amount, ..., gst, ..., amount, grand_total]
    # GST is typically 18% (or 5%, 28%) of amount.
    amount, gst_amount, total_amount = _extract_prices(raw_amounts)

    return {
        "part_category": part_categories[0] if part_categories else None,
        "part_category_alt": part_categories[1] if len(part_categories) > 1 else None,
        "uom": uom,
        "vendor_name": vendor_name,
        "vendor_location": vendor_location,
        "po_references": po_references,
        "progress_refs": progress_refs,
        "j_indent_refs": j_indent_refs,
        "status": status,
        "supply_type": supply_type,
        "images": images,
        "person_names": person_names,
        "dates": dates,
        "amount": amount,
        "gst_amount": gst_amount,
        "total_amount": total_amount,
    }


def _extract_prices(
    amounts: List[float],
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    From the raw float values in a segment, identify (amount, gst, total).

    Strategy (in order of preference):
      1. Standard GST triplet: T = A + G, G/A ≈ slab (5/12/18/28%).
      2. Any pair (A, T) where T = A * (1 + rate) within tolerance.
      3. Fallback: return the largest value as total_amount.

    Note: some indent parts have 3-component totals (material + labour + GST),
    in which case no 2-value GST pair exists.  We then return the maximum
    value as total_amount with amount/gst left as None.
    """
    if not amounts:
        return None, None, None

    unique = sorted(set(amounts))

    _GST_RATES = (0.05, 0.12, 0.18, 0.28)

    # Strategy 1: standard (A, G, T) triplet
    for A in unique:
        if A <= 0:
            continue
        for rate in _GST_RATES:
            G = A * rate
            T = A + G
            g_found = any(abs(x - G) < max(0.01, G * 0.005) for x in amounts)
            t_found = any(abs(x - T) < max(0.01, T * 0.005) for x in amounts)
            if g_found and t_found:
                gst_val = min(amounts, key=lambda x: abs(x - G))
                tot_val = min(amounts, key=lambda x: abs(x - T))
                return A, gst_val, tot_val

    # Strategy 2: any (A, T) pair where T ≈ A * (1 + rate)
    for A in unique:
        if A <= 0:
            continue
        for rate in _GST_RATES:
            T = A * (1 + rate)
            t_found = any(abs(x - T) < max(0.01, T * 0.005) for x in amounts)
            if t_found:
                tot_val = min(amounts, key=lambda x: abs(x - T))
                if tot_val != A:
                    return A, None, tot_val

    # Strategy 3: check if the largest value = sum of any two others (T = A + B)
    # This handles 3-component totals (material + labour + GST → total).
    if len(unique) >= 3:
        T = unique[-1]
        for i in range(len(unique) - 1):
            for j in range(i, len(unique) - 1):
                if abs(unique[i] + unique[j] - T) < max(0.05, T * 0.001):
                    return unique[j], unique[i], T

    # Fallback: return maximum as total_amount only
    if unique:
        return None, None, unique[-1]
    return None, None, None


# ── Detail response parser ────────────────────────────────────────────────────

def parse_detail_dump_text(text: str) -> Dict[str, Any]:
    """Parse a GetIndentPartsForProjectIndent GWT response.

    Returns {"parts": [...], "meta": {...}}.
    Each part dict contains:
      circuit_system, circuit_part_name, supply_type,
      indent_qty, approved_qty, dispatched_qty, remaining_qty,
      indent_date, required_date, part_category, uom,
      vendor_name, vendor_location, po_references, progress_refs,
      status, images, person_names, dates,
      amount, gst_amount, total_amount
    """
    data = normalize_gwt_response(text)
    if not isinstance(data, list):
        raise TypeError("Expected top-level GWT array")

    primitives, st, tail = split_primitive_stream_and_table(data)
    warnings: List[str] = []

    anchors = find_circuit_info_positions(primitives, st)
    parts: List[Dict[str, Any]] = []

    if not anchors:
        warnings.append("no_circuit_info_anchors_found")
        seen_circuits: set[str] = set()
        for s in st:
            if not _is_circuit_info(s) or s in seen_circuits:
                continue
            seen_circuits.add(s)
            circuit = parse_circuit_info(s)
            parts.append({
                "circuit_system": circuit.get("circuit_system"),
                "circuit_part_name": circuit.get("circuit_part_name"),
                "supply_type": circuit.get("supply_type"),
                "indent_qty": circuit.get("indent_qty"),
                "approved_qty": circuit.get("approved_qty"),
                "dispatched_qty": circuit.get("dispatched_qty"),
                "remaining_qty": circuit.get("remaining_qty"),
                "remarks": circuit.get("remarks"),
                "indent_date": circuit.get("indent_date"),
                "required_date": circuit.get("required_date"),
                "part_category": None,
                "part_category_alt": None,
                "uom": None,
                "vendor_name": None,
                "vendor_location": None,
                "po_references": [],
                "progress_refs": [],
                "j_indent_refs": [],
                "status": None,
                "images": [],
                "person_names": [],
                "dates": [],
                "amount": None,
                "gst_amount": None,
                "total_amount": None,
                "_circuit_raw": s,
            })
        if parts:
            warnings.append("used_string_table_fallback_for_parts")
    else:
        n = len(anchors)
        for idx, (pos, circuit_raw) in enumerate(anchors):
            end = anchors[idx + 1][0] if idx + 1 < n else len(primitives)
            # Small look-back (5 elements) to catch the part category string
            # that often appears just before the circuit-info reference.
            # Keep it tight to avoid bleeding numeric values from the previous part.
            look_back = max(0, pos - 5)
            segment = primitives[look_back:end]

            circuit = parse_circuit_info(circuit_raw)
            extra = extract_part_data_from_segment(segment, st)

            parts.append({
                # Circuit info fields
                "circuit_system": circuit.get("circuit_system"),
                "circuit_part_name": circuit.get("circuit_part_name"),
                "supply_type": extra.get("supply_type") or circuit.get("supply_type"),
                "indent_qty": circuit.get("indent_qty"),
                "approved_qty": circuit.get("approved_qty"),
                "dispatched_qty": circuit.get("dispatched_qty"),
                "remaining_qty": circuit.get("remaining_qty"),
                "remarks": circuit.get("remarks"),
                "indent_date": circuit.get("indent_date"),
                "required_date": circuit.get("required_date"),
                # Part info
                "part_category": extra.get("part_category"),
                "part_category_alt": extra.get("part_category_alt"),
                "uom": extra.get("uom"),
                # Commercial info
                "vendor_name": extra.get("vendor_name"),
                "vendor_location": extra.get("vendor_location"),
                "po_references": extra.get("po_references", []),
                "progress_refs": extra.get("progress_refs", []),
                "j_indent_refs": extra.get("j_indent_refs", []),
                "status": extra.get("status"),
                "images": extra.get("images", []),
                "person_names": extra.get("person_names", []),
                "dates": extra.get("dates", []),
                # Pricing
                "amount": extra.get("amount"),
                "gst_amount": extra.get("gst_amount"),
                "total_amount": extra.get("total_amount"),
                # Raw circuit info for reference
                "_circuit_raw": circuit_raw,
            })

    return {
        "parts": parts,
        "meta": {
            "string_table_len": len(st),
            "primitive_len": len(primitives),
            "anchor_count": len(anchors),
            "tail_preview": tail[:5] if tail else [],
            "warnings": warnings,
        },
    }


def parse_detail_dump_file(
    path: Path,
    indent_id: Optional[str] = None,
) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    result = parse_detail_dump_text(text)
    for part in result["parts"]:
        part["indent_id"] = indent_id
    result["source_dump"] = str(path)
    result["indent_id"] = indent_id
    return result


# ── List response parser ───────────────────────────────────────────────────────

def extract_indent_ids_from_list_response(text: str) -> List[int]:
    """
    Extract Java Long indent IDs from a getIndentListCompleted GWT response.

    Strategy: in GWT-RPC responses, java.lang.Long values are serialised as
    two consecutive 32-bit integers (high, low). We look for the java.lang.Long
    type descriptor in the string table, then scan for the pattern
      [long_type_ref, high, low]
    in the primitive stream.
    """
    data = normalize_gwt_response(text)
    if not isinstance(data, list):
        return []
    primitives, st, _ = split_primitive_stream_and_table(data)

    # Find ST index(es) of java.lang.Long
    long_indices: set[int] = set()
    for i, s in enumerate(st):
        if isinstance(s, str) and "java.lang.Long" in s:
            long_indices.add(i + 1)  # 1-based

    if not long_indices:
        return []

    ids: List[int] = []
    seen: set[int] = set()
    n = len(primitives)
    for i in range(n - 2):
        v = primitives[i]
        if v in long_indices:
            high = primitives[i + 1]
            low = primitives[i + 2]
            if (
                isinstance(high, int) and not isinstance(high, bool)
                and isinstance(low, int) and not isinstance(low, bool)
            ):
                hi = high & 0xFFFFFFFF
                lo = low & 0xFFFFFFFF
                value = (hi << 32) | lo
                if 1 <= value <= 9_999_999_999 and value not in seen:
                    ids.append(value)
                    seen.add(value)
    return ids


def parse_list_dump_text(text: str) -> Dict[str, Any]:
    """
    Parse a getIndentListCompleted GWT response.

    Extracts summary data from the string table:
      - J-indent references  (indent identifiers)
      - PO references        (purchase orders)
      - Part categories
      - Vendor names
      - Dates
      - Long IDs (for building detail request payloads)
    """
    data = normalize_gwt_response(text)
    if not isinstance(data, list):
        raise TypeError("Expected top-level GWT array")

    primitives, st, tail = split_primitive_stream_and_table(data)

    j_refs: List[str] = []
    po_refs: List[str] = []
    categories: List[str] = []
    vendors: List[str] = []
    dates: List[str] = []
    progress: List[str] = []

    for s in st:
        if not isinstance(s, str):
            continue
        if _JINDENT_RE.match(s):
            j_refs.append(s)
        elif _PO_RE.match(s):
            po_refs.append(s)
        elif _PROGRESS_RE.match(s):
            progress.append(s)
        elif _is_part_category(s):
            categories.append(s)
        elif _is_vendor(s):
            vendors.append(s)
        elif _DATE_RE.match(s):
            dates.append(s)

    indent_ids = extract_indent_ids_from_list_response(text)

    return {
        "j_indent_refs": j_refs,
        "po_references": po_refs,
        "progress_refs": progress,
        "part_categories": list(dict.fromkeys(categories)),
        "vendors": list(dict.fromkeys(vendors)),
        "dates": list(dict.fromkeys(dates)),
        "indent_ids": indent_ids,
        "meta": {
            "string_table_len": len(st),
            "primitive_len": len(primitives),
            "j_ref_count": len(j_refs),
            "indent_id_count": len(indent_ids),
        },
    }


def parse_list_dump_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    result = parse_list_dump_text(text)
    result["source_dump"] = str(path)
    return result


# ── Write helpers ─────────────────────────────────────────────────────────────

def write_parsed_detail(
    path: Path,
    out_path: Path,
    indent_id: Optional[str] = None,
) -> None:
    data = parse_detail_dump_file(path, indent_id=indent_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_parsed_list(path: Path, out_path: Path) -> None:
    data = parse_list_dump_file(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Aggregate ─────────────────────────────────────────────────────────────────

def aggregate_indent_parts(parsed_files: Sequence[Path]) -> Dict[str, Any]:
    """Merge all parsed detail files into a single output bundle."""
    all_parts: List[Dict[str, Any]] = []
    for p in parsed_files:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            all_parts.extend(doc.get("parts", []))
        except Exception:
            continue
    return {"total": len(all_parts), "parts": all_parts}
