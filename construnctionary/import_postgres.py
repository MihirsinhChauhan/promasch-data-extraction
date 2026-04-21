"""
Import Constructionary JSON bundles into PostgreSQL.

Primary key is always import_id (surrogate). The JSON "id" field is stored as
entity_ref for filtering/joins only — it is not unique and must not be used as PK.

Scalar fields are real columns; nested structures are JSONB for efficient indexing.

Connection (first match wins):
  - DATABASE_URL (e.g. postgresql://user:pass@localhost:5432/dbname)
  - libpq env: PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE

Examples:
  uv run python import_postgres.py --final-dir data/final --create-table
  uv run python import_postgres.py --final-dir data/final --recreate-table --truncate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import psycopg
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parent

DEFAULT_BATCH_INSERT = 500

# JSON keys we map to columns; anything else goes to extras JSONB.
_MAPPED_KEYS = frozenset(
    {
        "id",
        "brand",
        "model",
        "display_name",
        "price_last_purchase",
        "price_market",
        "purchase_qty",
        "vendor_count",
        "images",
        "specifications",
        "vendors",
        "location",
        "category_path",
        "created_by",
    }
)

DDL_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS constructionary_parts (
    import_id           BIGSERIAL PRIMARY KEY,
    source_file         TEXT NOT NULL,
    entity_ref          TEXT,
    brand               TEXT,
    model               TEXT,
    display_name        TEXT,
    price_last_purchase DOUBLE PRECISION,
    price_market        DOUBLE PRECISION,
    purchase_qty        DOUBLE PRECISION,
    vendor_count        INTEGER,
    location            TEXT,
    created_by          TEXT,
    images              JSONB NOT NULL DEFAULT '[]'::jsonb,
    specifications      JSONB NOT NULL DEFAULT '{}'::jsonb,
    vendors             JSONB NOT NULL DEFAULT '[]'::jsonb,
    category_path       JSONB NOT NULL DEFAULT '[]'::jsonb,
    extras              JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_entity_ref
    ON constructionary_parts (entity_ref)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_source_file
    ON constructionary_parts (source_file)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_brand_model
    ON constructionary_parts (brand, model)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_location
    ON constructionary_parts (location)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_category_path_gin
    ON constructionary_parts USING GIN (category_path)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_vendors_gin
    ON constructionary_parts USING GIN (vendors)
""",
    """
CREATE INDEX IF NOT EXISTS idx_constructionary_parts_specifications_gin
    ON constructionary_parts USING GIN (specifications)
""",
]


def apply_ddl(conn: psycopg.Connection) -> None:
    for stmt in DDL_STATEMENTS:
        conn.execute(stmt.strip())


def connect_dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", os.environ.get("USER", "postgres"))
    password = os.environ.get("PGPASSWORD", "")
    db = os.environ.get("PGDATABASE", "postgres")
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _jsonb_list(v: Any) -> Json:
    if isinstance(v, list):
        return Json(v)
    return Json([])


def _jsonb_dict(v: Any) -> Json:
    if isinstance(v, dict):
        return Json(v)
    return Json({})


def row_from_part(source_file: str, part: Dict[str, Any]) -> Tuple[Any, ...]:
    """Map one JSON part object to a DB row (excluding import_id)."""
    raw_id = part.get("id")
    entity_ref = _opt_str(raw_id) if raw_id is not None else None

    extras = {k: v for k, v in part.items() if k not in _MAPPED_KEYS}

    return (
        source_file,
        entity_ref,
        _opt_str(part.get("brand")),
        _opt_str(part.get("model")),
        _opt_str(part.get("display_name")),
        _opt_float(part.get("price_last_purchase")),
        _opt_float(part.get("price_market")),
        _opt_float(part.get("purchase_qty")),
        _opt_int(part.get("vendor_count")),
        _opt_str(part.get("location")),
        _opt_str(part.get("created_by")),
        _jsonb_list(part.get("images")),
        _jsonb_dict(part.get("specifications")),
        _jsonb_list(part.get("vendors")),
        _jsonb_list(part.get("category_path")),
        Json(extras),
    )


INSERT_SQL = """
INSERT INTO constructionary_parts (
    source_file,
    entity_ref,
    brand,
    model,
    display_name,
    price_last_purchase,
    price_market,
    purchase_qty,
    vendor_count,
    location,
    created_by,
    images,
    specifications,
    vendors,
    category_path,
    extras
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s,
    %s, %s, %s, %s,
    %s
)
"""


def discover_final_files(
    final_dir: Path,
    *,
    exclude_merged: bool,
) -> List[Path]:
    out: List[Path] = []
    for p in sorted(final_dir.glob("constructionary_*.json")):
        if exclude_merged and p.name == "constructionary_merged.json":
            continue
        out.append(p)
    for name in ("gap_fill.json",):
        p = final_dir / name
        if p.is_file():
            out.append(p)
    return sorted(set(out), key=lambda x: str(x))


def discover_batch_files(batch_parent: Path) -> List[Path]:
    files: List[Path] = []
    for batch in sorted(batch_parent.glob("batch_*")):
        if not batch.is_dir():
            continue
        out = batch / "output.json"
        if out.is_file():
            files.append(out)
    return files


def load_bundle(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    rel = str(path)
    try:
        rel = str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        pass
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected object at top level")
    parts = data.get("parts")
    if not isinstance(parts, list):
        raise ValueError(f"{path}: missing or invalid 'parts' array")
    return rel, parts


def batched(
    rows: Iterable[Tuple[Any, ...]],
    size: int,
) -> Iterator[List[Tuple[Any, ...]]]:
    batch: List[Tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def iter_rows_from_files(files: List[Path]) -> Iterator[Tuple[Any, ...]]:
    for path in files:
        source_label, parts = load_bundle(path)
        for part in parts:
            if not isinstance(part, dict):
                continue
            yield row_from_part(source_label, part)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--final-dir",
        type=Path,
        default=ROOT / "data" / "final",
        help="Directory with constructionary_*.json and gap_fill.json (default: api-extraction/data/final)",
    )
    p.add_argument(
        "--include-batches",
        action="store_true",
        help="Also import data/*/batch_*/output.json under --batch-parent",
    )
    p.add_argument(
        "--batch-parent",
        type=Path,
        default=None,
        help="Parent of batch_* folders (default: parent of --final-dir)",
    )
    p.add_argument(
        "--exclude-merged",
        action="store_true",
        help="Skip constructionary_merged.json (avoids duplicate rows vs shards)",
    )
    p.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL connection URI (overrides DATABASE_URL / PG* env)",
    )
    p.add_argument(
        "--create-table",
        action="store_true",
        help="Run DDL (CREATE TABLE + indexes) before import",
    )
    p.add_argument(
        "--recreate-table",
        action="store_true",
        help="DROP TABLE constructionary_parts then CREATE (implies --create-table)",
    )
    p.add_argument(
        "--truncate",
        action="store_true",
        help="DELETE FROM constructionary_parts before import",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_INSERT,
        metavar="N",
        help=f"Rows per INSERT batch (default {DEFAULT_BATCH_INSERT})",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    final_dir = args.final_dir.expanduser().resolve()
    if not final_dir.is_dir():
        print(f"error: --final-dir not a directory: {final_dir}", file=sys.stderr)
        return 1

    files = discover_final_files(final_dir, exclude_merged=args.exclude_merged)
    batch_parent = args.batch_parent
    if args.include_batches:
        bp = (batch_parent or final_dir.parent).expanduser().resolve()
        if not bp.is_dir():
            print(f"error: --batch-parent not a directory: {bp}", file=sys.stderr)
            return 1
        files.extend(discover_batch_files(bp))

    if not files:
        print("error: no JSON files matched", file=sys.stderr)
        return 1

    dsn = args.dsn or connect_dsn()
    create = args.create_table or args.recreate_table

    total = 0
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT 1")  # fail fast if DB unreachable
        if args.recreate_table:
            conn.execute("DROP TABLE IF EXISTS constructionary_parts CASCADE")
        if create:
            apply_ddl(conn)
        if args.truncate and not args.recreate_table:
            conn.execute("DELETE FROM constructionary_parts")
        conn.commit()

        with conn.cursor() as cur:
            for chunk in batched(iter_rows_from_files(files), max(1, args.batch_size)):
                cur.executemany(INSERT_SQL, chunk)
                total += len(chunk)
        conn.commit()

    print(f"Imported {total} rows from {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
