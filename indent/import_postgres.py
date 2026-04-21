"""
Import Indent Completed JSON bundles into PostgreSQL.

Table: indent_parts
  Primary key: import_id (surrogate BIGSERIAL).
  Scalar fields are real columns; nested structures (images, po_references,
  progress_refs, person_names) are stored as JSONB.

Connection (first match wins):
  DATABASE_URL  e.g. postgresql://user:pass@localhost:5432/dbname
  PG* env vars  PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE

Examples:
  uv run python import_postgres.py --output-dir data/1234/output.json --create-table
  uv run python import_postgres.py --final-dir  data/final --recreate-table
  uv run python import_postgres.py --final-dir  data/final --truncate
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

# ── Column mapping ─────────────────────────────────────────────────────────────
# Keys present here are mapped to dedicated columns; everything else → extras JSONB.
_MAPPED_KEYS = frozenset({
    "indent_id",
    "circuit_system",
    "circuit_part_name",
    "supply_type",
    "indent_qty",
    "approved_qty",
    "dispatched_qty",
    "remaining_qty",
    "remarks",
    "indent_date",
    "required_date",
    "part_category",
    "part_category_alt",
    "uom",
    "vendor_name",
    "vendor_location",
    "status",
    "amount",
    "gst_amount",
    "total_amount",
    # JSONB columns (lists)
    "po_references",
    "progress_refs",
    "j_indent_refs",
    "images",
    "person_names",
    "dates",
})

# ── DDL ────────────────────────────────────────────────────────────────────────

DDL_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS indent_parts (
    import_id           BIGSERIAL PRIMARY KEY,
    source_file         TEXT        NOT NULL,
    indent_id           TEXT,
    circuit_system      TEXT,
    circuit_part_name   TEXT,
    supply_type         TEXT,
    indent_qty          DOUBLE PRECISION,
    approved_qty        DOUBLE PRECISION,
    dispatched_qty      DOUBLE PRECISION,
    remaining_qty       DOUBLE PRECISION,
    remarks             TEXT,
    indent_date         TEXT,
    required_date       TEXT,
    part_category       TEXT,
    part_category_alt   TEXT,
    uom                 TEXT,
    vendor_name         TEXT,
    vendor_location     TEXT,
    status              TEXT,
    amount              DOUBLE PRECISION,
    gst_amount          DOUBLE PRECISION,
    total_amount        DOUBLE PRECISION,
    po_references       JSONB NOT NULL DEFAULT '[]'::jsonb,
    progress_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    j_indent_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    images              JSONB NOT NULL DEFAULT '[]'::jsonb,
    person_names        JSONB NOT NULL DEFAULT '[]'::jsonb,
    dates               JSONB NOT NULL DEFAULT '[]'::jsonb,
    extras              JSONB NOT NULL DEFAULT '{}'::jsonb,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_indent_id
    ON indent_parts (indent_id)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_source_file
    ON indent_parts (source_file)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_circuit_system
    ON indent_parts (circuit_system)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_vendor
    ON indent_parts (vendor_name)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_status
    ON indent_parts (status)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_po_references_gin
    ON indent_parts USING GIN (po_references)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_images_gin
    ON indent_parts USING GIN (images)
""",
    """
CREATE INDEX IF NOT EXISTS idx_indent_parts_progress_refs_gin
    ON indent_parts USING GIN (progress_refs)
""",
]

# ── Type coercions ─────────────────────────────────────────────────────────────

def _opt_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
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


# ── Row mapper ─────────────────────────────────────────────────────────────────

INSERT_SQL = """
INSERT INTO indent_parts (
    source_file,
    indent_id,
    circuit_system,
    circuit_part_name,
    supply_type,
    indent_qty,
    approved_qty,
    dispatched_qty,
    remaining_qty,
    remarks,
    indent_date,
    required_date,
    part_category,
    part_category_alt,
    uom,
    vendor_name,
    vendor_location,
    status,
    amount,
    gst_amount,
    total_amount,
    po_references,
    progress_refs,
    j_indent_refs,
    images,
    person_names,
    dates,
    extras
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s, %s,
    %s
)
"""


def row_from_part(source_file: str, part: Dict[str, Any]) -> Tuple[Any, ...]:
    """Map one JSON part object to a DB row tuple (excluding import_id)."""
    extras = {k: v for k, v in part.items() if k not in _MAPPED_KEYS and not k.startswith("_")}
    return (
        source_file,
        _opt_str(part.get("indent_id")),
        _opt_str(part.get("circuit_system")),
        _opt_str(part.get("circuit_part_name")),
        _opt_str(part.get("supply_type")),
        _opt_float(part.get("indent_qty")),
        _opt_float(part.get("approved_qty")),
        _opt_float(part.get("dispatched_qty")),
        _opt_float(part.get("remaining_qty")),
        _opt_str(part.get("remarks")),
        _opt_str(part.get("indent_date")),
        _opt_str(part.get("required_date")),
        _opt_str(part.get("part_category")),
        _opt_str(part.get("part_category_alt")),
        _opt_str(part.get("uom")),
        _opt_str(part.get("vendor_name")),
        _opt_str(part.get("vendor_location")),
        _opt_str(part.get("status")),
        _opt_float(part.get("amount")),
        _opt_float(part.get("gst_amount")),
        _opt_float(part.get("total_amount")),
        _jsonb_list(part.get("po_references")),
        _jsonb_list(part.get("progress_refs")),
        _jsonb_list(part.get("j_indent_refs")),
        _jsonb_list(part.get("images")),
        _jsonb_list(part.get("person_names")),
        _jsonb_list(part.get("dates")),
        Json(extras),
    )


# ── File discovery ─────────────────────────────────────────────────────────────

def discover_output_files(final_dir: Path) -> List[Path]:
    files: List[Path] = []
    for p in sorted(final_dir.glob("*.json")):
        files.append(p)
    return files


def discover_batch_output_files(batch_parent: Path) -> List[Path]:
    files: List[Path] = []
    for batch in sorted(batch_parent.glob("batch_*")):
        if not batch.is_dir():
            continue
        out = batch / "output.json"
        if out.is_file():
            files.append(out)
    return files


def load_bundle(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        rel = str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        rel = str(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected JSON object at top level")
    parts = data.get("parts")
    if not isinstance(parts, list):
        raise ValueError(f"{path}: missing or invalid 'parts' array")
    return rel, parts


# ── Batch insert ───────────────────────────────────────────────────────────────

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


def iter_rows_from_files(
    files: List[Path],
) -> Iterator[Tuple[Any, ...]]:
    for path in files:
        source_label, parts = load_bundle(path)
        for part in parts:
            if not isinstance(part, dict):
                continue
            yield row_from_part(source_label, part)


# ── DB connection ──────────────────────────────────────────────────────────────

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


def apply_ddl(conn: psycopg.Connection) -> None:
    for stmt in DDL_STATEMENTS:
        conn.execute(stmt.strip())


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--output-file",
        type=Path,
        metavar="PATH",
        help="Single output.json file to import",
    )
    grp.add_argument(
        "--final-dir",
        type=Path,
        metavar="DIR",
        help="Directory containing output.json files (glob *.json)",
    )
    p.add_argument(
        "--include-batches",
        action="store_true",
        help="Also import batch_*/output.json under --batch-parent",
    )
    p.add_argument(
        "--batch-parent",
        type=Path,
        default=None,
        help="Parent of batch_* dirs (default: parent of --final-dir)",
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
        help="DROP TABLE indent_parts then CREATE (implies --create-table)",
    )
    p.add_argument(
        "--truncate",
        action="store_true",
        help="DELETE FROM indent_parts before import",
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

    if args.output_file:
        files = [args.output_file.expanduser().resolve()]
        if not files[0].is_file():
            print(f"error: file not found: {files[0]}", file=sys.stderr)
            return 1
    else:
        final_dir = args.final_dir.expanduser().resolve()
        if not final_dir.is_dir():
            print(f"error: --final-dir not a directory: {final_dir}", file=sys.stderr)
            return 1
        files = discover_output_files(final_dir)

    if args.include_batches:
        bp = (
            args.batch_parent or (
                args.final_dir.expanduser().resolve().parent
                if args.final_dir else Path.cwd()
            )
        ).expanduser().resolve()
        if not bp.is_dir():
            print(f"error: --batch-parent not a directory: {bp}", file=sys.stderr)
            return 1
        files.extend(discover_batch_output_files(bp))

    if not files:
        print("error: no JSON files matched", file=sys.stderr)
        return 1

    dsn = args.dsn or connect_dsn()
    create = args.create_table or args.recreate_table

    total = 0
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT 1")
        if args.recreate_table:
            conn.execute("DROP TABLE IF EXISTS indent_parts CASCADE")
        if create:
            apply_ddl(conn)
        if args.truncate and not args.recreate_table:
            conn.execute("DELETE FROM indent_parts")
        conn.commit()

        with conn.cursor() as cur:
            for chunk in batched(
                iter_rows_from_files(files), max(1, args.batch_size)
            ):
                cur.executemany(INSERT_SQL, chunk)
                total += len(chunk)
        conn.commit()

    print(f"Imported {total} rows from {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
