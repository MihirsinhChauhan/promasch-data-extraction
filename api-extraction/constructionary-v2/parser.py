"""Constructionary v2 parser that treats `display_name` as non-unique metadata."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import json5

DISPLAY_NAME_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")
_GWT_SENTINELS = frozenset(range(-30, 0))
_CONCAT_RE = re.compile(r'\]\s*\.concat\s*\(')
_BACKEND_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,}$")


def _collapse_concat_arrays(text: str) -> str:
    while True:
        m = _CONCAT_RE.search(text)
        if not m:
            break

        j = m.end()
        n = len(text)
        parts: list[str] = []

        while j < n:
            while j < n and text[j] in " \t\n\r,":
                j += 1
            if j >= n:
                break
            if text[j] == ")":
                j += 1
                break
            if text[j] == "[":
                j += 1
                start = j
                depth = 1
                in_sq = in_dq = False
                while j < n and depth > 0:
                    ch = text[j]
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
                parts.append(text[start : j - 1])
            else:
                start = j
                while j < n and text[j] not in ",)":
                    j += 1
                parts.append(text[start:j])

        text = text[: m.start()] + "," + ",".join(parts) + "]" + text[j:]

    return text


def normalize_gwt_response(text: str) -> List[Any]:
    t = text.strip()
    if t.startswith("//EX"):
        raise ValueError(f"GWT exception response: {t[:500]}")
    if t.startswith("//OK"):
        t = t[4:].strip()
    t = _collapse_concat_arrays(t)
    return json5.loads(t)


def _looks_like_string_table_cell(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("http://") or value.startswith("https://"):
        return True
    if "/" in value and ("java." in value or "com.l3" in value):
        return True
    if len(value) > 80:
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


def split_primitive_stream_and_table(data: List[Any]) -> Tuple[List[Any], List[str], List[Any]]:
    st_idx = _find_string_table_index(data)
    if st_idx is None:
        return list(data), [], []
    return list(data[:st_idx]), [str(x) for x in data[st_idx]], list(data[st_idx + 1 :])


def parse_display_name(name: str) -> Dict[str, str]:
    m = DISPLAY_NAME_RE.match(name.strip())
    if not m:
        return {}
    return {
        "brand": m.group(1).strip(),
        "model": m.group(2).strip(),
        "id": f"{m.group(2)}.{m.group(3)}",
    }


def find_display_name_positions(primitives: Sequence[Any], st: List[str]) -> List[Tuple[int, str, int]]:
    result: List[Tuple[int, str, int]] = []
    for i, v in enumerate(primitives):
        if not isinstance(v, int) or not (1 <= v <= len(st)):
            continue
        candidate = st[v - 1]
        if isinstance(candidate, str) and DISPLAY_NAME_RE.match(candidate.strip()):
            result.append((i, candidate.strip(), v))
    return result


def _is_backend_identifier(value: str) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or " " in v:
        return False
    if v.startswith("http") or v.startswith("java.") or v.startswith("com."):
        return False
    if DISPLAY_NAME_RE.match(v):
        return False
    return bool(_BACKEND_REF_RE.match(v))


def _extract_entity_ref(
    primitives: Sequence[Any],
    st: List[str],
    display_pos: int,
    display_st_ref: int,
) -> str:
    candidates: List[Tuple[int, str]] = []
    start = max(0, display_pos - 20)
    end = min(len(primitives), display_pos + 20)
    for i in range(start, end):
        v = primitives[i]
        if not isinstance(v, int) or not (1 <= v <= len(st)):
            continue
        if i == display_pos and v == display_st_ref:
            continue
        ref = st[v - 1]
        if _is_backend_identifier(ref):
            distance = abs(i - display_pos)
            candidates.append((distance, ref))
    if candidates:
        candidates.sort(key=lambda x: (x[0], x[1]))
        return candidates[0][1]
    return f"st_ref:{display_st_ref}"


def extract_part_data_from_segment(segment: Sequence[Any], st: List[str]) -> Dict[str, Any]:
    images: List[str] = []
    price_candidates: List[float] = []

    st_len = len(st)
    for v in segment:
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= st_len:
            ref = st[v - 1]
            if isinstance(ref, str) and ref.startswith("http") and ref not in images:
                images.append(ref)
            continue

        if (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and v not in _GWT_SENTINELS
            and v > 0
        ):
            fv = float(v)
            if fv > 100:
                price_candidates.append(fv)

    unique_prices = sorted(set(price_candidates))
    price_last_purchase = unique_prices[0] if unique_prices else 0.0
    price_market = unique_prices[-1] if unique_prices else 0.0

    return {
        "price_last_purchase": price_last_purchase,
        "price_market": price_market,
        "images": images,
    }


def parse_dump_text(text: str, *, source_dump: str = "") -> Dict[str, Any]:
    data = normalize_gwt_response(text)
    if not isinstance(data, list):
        raise TypeError("Expected top-level GWT array")

    primitives, st, tail = split_primitive_stream_and_table(data)
    display_refs = find_display_name_positions(primitives, st)

    parts: List[Dict[str, Any]] = []
    for record_index, (pos, name, st_ref) in enumerate(display_refs):
        end = display_refs[record_index + 1][0] if record_index + 1 < len(display_refs) else len(primitives)
        segment = primitives[pos:end]

        meta = parse_display_name(name)
        entity_ref = _extract_entity_ref(primitives, st, pos, st_ref)
        extra = extract_part_data_from_segment(segment, st)
        part_key = f"{entity_ref}|{source_dump}|{record_index}"

        parts.append(
            {
                **meta,
                "entity_ref": entity_ref,
                "display_name": name,
                "source_dump": source_dump,
                "record_index_in_dump": record_index,
                "part_key": part_key,
                "price_last_purchase": extra["price_last_purchase"],
                "price_market": extra["price_market"],
                "images": extra["images"],
            }
        )

    return {
        "parts": parts,
        "meta": {
            "string_table_len": len(st),
            "primitive_len": len(primitives),
            "display_name_count": len(display_refs),
            "tail_preview": tail[:5] if tail else [],
        },
    }


def parse_dump_file(path: Path) -> Dict[str, Any]:
    source_dump = str(path)
    result = parse_dump_text(path.read_text(encoding="utf-8"), source_dump=source_dump)
    return result


def aggregate_parts(
    parsed_files: Sequence[Path],
    *,
    metrics_path: Path = Path("logs/identity_metrics.json"),
) -> Dict[str, Any]:
    merged_by_part_key: Dict[str, Dict[str, Any]] = {}
    display_to_entity_refs: Dict[str, set[str]] = {}
    total_records = 0

    for parsed_file in parsed_files:
        try:
            doc = json.loads(parsed_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        for part in doc.get("parts", []):
            total_records += 1
            part_key = part.get("part_key")
            if not part_key:
                entity_ref = part.get("entity_ref", "")
                source_dump = part.get("source_dump", "")
                idx = part.get("record_index_in_dump")
                part_key = f"{entity_ref}|{source_dump}|{idx}"
                part["part_key"] = part_key

            merged_by_part_key[part_key] = part

            display_name = part.get("display_name")
            entity_ref = str(part.get("entity_ref", ""))
            if display_name:
                display_to_entity_refs.setdefault(display_name, set()).add(entity_ref)

    collision_names = {
        name: sorted(refs)
        for name, refs in display_to_entity_refs.items()
        if len({r for r in refs if r}) > 1
    }

    metrics = {
        "total_records_seen": total_records,
        "total_records_after_part_key_dedupe": len(merged_by_part_key),
        "display_name_collision_count": len(collision_names),
        "display_name_collisions": collision_names,
    }
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "total": len(merged_by_part_key),
        "parts": list(merged_by_part_key.values()),
        "metrics_path": str(metrics_path),
    }
