"""
Import constructionary_v2 records from parts_records.jsonl into PostgreSQL.

Table: constructionary_v2_parts
  Primary key: entity_id (BIGINT, from getStockForStockroom response or sha1-derived)
  entity_id_source: 'response' | 'hashed'  (so we can identify unreliable PKs later)

Connection (first match wins):
  DATABASE_URL env var, or libpq env vars (PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE)

Examples:
  python import_postgres.py --create-table
  python import_postgres.py --recreate-table
  python import_postgres.py --upsert            # on conflict entity_id do update
  python import_postgres.py --limit 100         # test with a subset
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import psycopg
from psycopg.types.json import Json

import config
from utils import load_jsonl, setup_logging

log = setup_logging("import_postgres")

DEFAULT_BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS constructionary_v2_parts (
    entity_id           BIGINT       NOT NULL,
    entity_id_source    TEXT         NOT NULL DEFAULT 'hashed',
    entity_ref          TEXT,
    display_name        TEXT,
    brand               TEXT,
    model               TEXT,
    category_path       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    price_market        DOUBLE PRECISION,
    price_last_purchase DOUBLE PRECISION,
    purchase_qty        DOUBLE PRECISION,
    stock_qty           DOUBLE PRECISION,
    total_value         DOUBLE PRECISION,
    uom                 TEXT,
    specifications      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    vendors             JSONB        NOT NULL DEFAULT '[]'::jsonb,
    marketplace_vendors JSONB        NOT NULL DEFAULT '[]'::jsonb,
    image_source_urls   TEXT[]       NOT NULL DEFAULT '{}',
    image_s3_urls       TEXT[]       NOT NULL DEFAULT '{}',
    created_by          TEXT,
    used_in             JSONB        NOT NULL DEFAULT '[]'::jsonb,
    documents           JSONB        NOT NULL DEFAULT '[]'::jsonb,
    parse_warnings      JSONB        NOT NULL DEFAULT '[]'::jsonb,
    extras              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    imported_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT constructionary_v2_parts_pkey PRIMARY KEY (entity_id)
)
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_cv2_entity_ref ON constructionary_v2_parts (entity_ref)",
    "CREATE INDEX IF NOT EXISTS idx_cv2_brand_model ON constructionary_v2_parts (brand, model)",
    "CREATE INDEX IF NOT EXISTS idx_cv2_entity_id_source ON constructionary_v2_parts (entity_id_source)",
    "CREATE INDEX IF NOT EXISTS idx_cv2_category_path_gin ON constructionary_v2_parts USING GIN (category_path)",
    "CREATE INDEX IF NOT EXISTS idx_cv2_specifications_gin ON constructionary_v2_parts USING GIN (specifications)",
    "CREATE INDEX IF NOT EXISTS idx_cv2_vendors_gin ON constructionary_v2_parts USING GIN (vendors)",
]

# Columns whose values come directly from the record dict
_MAPPED_KEYS = frozenset({
    "display_name", "entity_ref", "entity_id", "entity_id_source",
    "brand", "model", "category_path",
    "price_market", "price_last_purchase", "purchase_qty", "stock_qty", "total_value",
    "uom", "specifications", "vendors", "marketplace_vendors",
    "image_urls", "image_s3_urls",
    "created_by", "used_in", "documents",
    "_parse_warnings",
})

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO constructionary_v2_parts (
    entity_id, entity_id_source, entity_ref, display_name, brand, model,
    category_path, price_market, price_last_purchase, purchase_qty, stock_qty,
    total_value, uom, specifications, vendors, marketplace_vendors,
    image_source_urls, image_s3_urls, created_by, used_in, documents,
    parse_warnings, extras
) VALUES (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s
)
"""

_UPSERT_SQL = _INSERT_SQL.rstrip() + """
ON CONFLICT (entity_id) DO UPDATE SET
    entity_id_source    = EXCLUDED.entity_id_source,
    entity_ref          = EXCLUDED.entity_ref,
    display_name        = EXCLUDED.display_name,
    brand               = EXCLUDED.brand,
    model               = EXCLUDED.model,
    category_path       = EXCLUDED.category_path,
    price_market        = EXCLUDED.price_market,
    price_last_purchase = EXCLUDED.price_last_purchase,
    purchase_qty        = EXCLUDED.purchase_qty,
    stock_qty           = EXCLUDED.stock_qty,
    total_value         = EXCLUDED.total_value,
    uom                 = EXCLUDED.uom,
    specifications      = EXCLUDED.specifications,
    vendors             = EXCLUDED.vendors,
    marketplace_vendors = EXCLUDED.marketplace_vendors,
    image_source_urls   = EXCLUDED.image_source_urls,
    image_s3_urls       = EXCLUDED.image_s3_urls,
    created_by          = EXCLUDED.created_by,
    used_in             = EXCLUDED.used_in,
    documents           = EXCLUDED.documents,
    parse_warnings      = EXCLUDED.parse_warnings,
    extras              = EXCLUDED.extras,
    imported_at         = NOW()
"""

# ---------------------------------------------------------------------------
# Type coercions
# ---------------------------------------------------------------------------

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
    return Json(v if isinstance(v, list) else [])


def _jsonb_dict(v: Any) -> Json:
    return Json(v if isinstance(v, dict) else {})


def _text_array(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return []


def _entity_id_int(record: dict) -> int:
    """
    Convert entity_id to a Postgres BIGINT.
    - If it's already an int, use it directly.
    - If it's a hex string (hashed), take the first 15 hex digits → int (fits BIGINT).
    """
    eid = record.get("entity_id")
    if isinstance(eid, int):
        return eid
    if isinstance(eid, str):
        # Hashed entity_id is a 40-char hex sha1; truncate to 15 hex digits → ~60-bit int
        return int(eid[:15], 16)
    raise ValueError(f"Unexpected entity_id type: {type(eid)!r} value={eid!r}")


def row_from_record(record: dict) -> tuple[Any, ...]:
    extras = {k: v for k, v in record.items() if k not in _MAPPED_KEYS}

    return (
        _entity_id_int(record),
        _opt_str(record.get("entity_id_source")) or "hashed",
        _opt_str(record.get("entity_ref")),
        _opt_str(record.get("display_name")),
        _opt_str(record.get("brand")),
        _opt_str(record.get("model")),
        _jsonb_list(record.get("category_path")),
        _opt_float(record.get("price_market")),
        _opt_float(record.get("price_last_purchase")),
        _opt_float(record.get("purchase_qty")),
        _opt_float(record.get("stock_qty")),
        _opt_float(record.get("total_value")),
        _opt_str(record.get("uom")),
        _jsonb_dict(record.get("specifications")),
        _jsonb_list(record.get("vendors")),
        _jsonb_list(record.get("marketplace_vendors")),
        _text_array(record.get("image_urls")),
        _text_array(record.get("image_s3_urls")),
        _opt_str(record.get("created_by")),
        _jsonb_list(record.get("used_in")),
        _jsonb_list(record.get("documents")),
        _jsonb_list(record.get("_parse_warnings")),
        Json(extras),
    )


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Batch iterator
# ---------------------------------------------------------------------------

def _batched(
    rows: Iterable[tuple[Any, ...]],
    size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_import(
    *,
    data_dir: Path,
    dsn: Optional[str] = None,
    create_table: bool = False,
    recreate_table: bool = False,
    truncate: bool = False,
    upsert: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: Optional[int] = None,
) -> int:
    records_path = data_dir / "parts_records.jsonl"
    records = load_jsonl(records_path)

    if not records:
        log.error("parts_records.jsonl is empty or missing in %s", data_dir)
        return 0

    if limit is not None:
        records = records[:limit]

    log.info("[import] Loading %d records from %s", len(records), records_path)

    sql = _UPSERT_SQL if upsert else _INSERT_SQL
    dsn = dsn or connect_dsn()
    create = create_table or recreate_table

    total_inserted = 0
    total_errors = 0

    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT 1")  # fail fast
        if recreate_table:
            conn.execute("DROP TABLE IF EXISTS constructionary_v2_parts CASCADE")
            log.info("[import] Dropped constructionary_v2_parts")
        if create:
            conn.execute(_DDL_TABLE.strip())
            for idx_sql in _DDL_INDEXES:
                conn.execute(idx_sql.strip())
            log.info("[import] Table + indexes created")
        if truncate and not recreate_table:
            conn.execute("DELETE FROM constructionary_v2_parts")
            log.info("[import] Truncated table")
        conn.commit()

        def _iter_rows():
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                try:
                    yield row_from_record(rec)
                except Exception as e:
                    nonlocal total_errors
                    dn = rec.get("display_name", "?")[:50]
                    log.warning("[import] Skipping row for %s: %s", dn, e)
                    total_errors += 1

        with conn.cursor() as cur:
            for batch in _batched(_iter_rows(), batch_size):
                try:
                    cur.executemany(sql, batch)
                    total_inserted += len(batch)
                    if total_inserted % 1000 == 0:
                        log.info("[import] %d rows inserted so far", total_inserted)
                except Exception as e:
                    log.error("[import] Batch insert failed: %s — rolling back batch", e)
                    conn.rollback()
                    total_errors += len(batch)
        conn.commit()

    log.info(
        "[import] Done: %d rows inserted/upserted, %d errors.",
        total_inserted, total_errors,
    )
    return total_inserted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    p.add_argument("--dsn", default=None, help="Postgres URI (overrides DATABASE_URL / PG* env)")
    p.add_argument("--create-table", action="store_true", help="CREATE TABLE + indexes before import")
    p.add_argument("--recreate-table", action="store_true", help="DROP then CREATE table (implies --create-table)")
    p.add_argument("--truncate", action="store_true", help="DELETE all rows before import")
    p.add_argument("--upsert", action="store_true", help="ON CONFLICT (entity_id) DO UPDATE")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, metavar="N")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="Import only first N records (for testing)")
    args = p.parse_args()

    n = run_import(
        data_dir=args.data_dir.expanduser().resolve(),
        dsn=args.dsn,
        create_table=args.create_table,
        recreate_table=args.recreate_table,
        truncate=args.truncate,
        upsert=args.upsert,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    print(f"Imported {n} rows.")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
