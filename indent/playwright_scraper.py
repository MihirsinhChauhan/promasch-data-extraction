"""
Indent scraper orchestration.

Runs:
  1) Playwright capture
  2) Optional detail-payload generation
  3) Replay
  4) Parse detail dumps
  5) Build indent_catalog.json-compatible records
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import config
from collector import (
    build_detail_payloads_from_catalog,
    ensure_credentials,
    run_collection,
)
from gwt_parser import aggregate_indent_parts, parse_detail_dump_file
from replay import replay_catalog
from utils import save_json, setup_logging

log = setup_logging("scraper")

_DETAIL_METHOD = "GetIndentPartsForProjectIndent"


def _load_catalog(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "payload_catalog.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_detail_docs(data_dir: Path, workers: int) -> list[dict[str, Any]]:
    dumps_dir = data_dir / "dumps"
    parsed_dir = data_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    id_lookup: dict[str, str | None] = {}
    for entry in _load_catalog(data_dir):
        if entry.get("method") == _DETAIL_METHOD:
            id_lookup[Path(entry["dump"]).stem] = entry.get("indent_id")

    dump_files = [f for f in sorted(dumps_dir.glob("*.txt")) if f.stem in id_lookup]
    if not dump_files:
        log.warning("No detail dumps found in %s", dumps_dir)
        return []

    def work(df: Path) -> dict[str, Any] | None:
        try:
            doc = parse_detail_dump_file(df, indent_id=id_lookup.get(df.stem))
            if not doc.get("parts"):
                return None
            out = parsed_dir / f"{df.stem}.json"
            out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            return doc
        except Exception as e:
            log.warning("Failed parsing %s: %s", df.name, e)
            return None

    docs: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for doc in ex.map(work, dump_files):
            if doc:
                docs.append(doc)

    return docs


def _catalog_from_docs(docs: list[dict[str, Any]], *, run_id: str, data_dir: Path) -> list[dict[str, Any]]:
    by_indent: dict[str, dict[str, Any]] = {}
    for doc in docs:
        indent_id = doc.get("indent_id")
        if not indent_id:
            continue
        parts = doc.get("parts", [])
        row = by_indent.setdefault(indent_id, {
            "indent_id": indent_id,
            "run_id": run_id,
            "source_data_dir": str(data_dir),
            "part_count": 0,
            "po_references": [],
            "j_indent_refs": [],
            "pdf_url": (
                config.INDENT_PDF_URL_TEMPLATE.format(indent_id=indent_id)
                if config.INDENT_PDF_URL_TEMPLATE else ""
            ),
        })
        row["part_count"] += len(parts)
        for part in parts:
            for key in ("po_references", "j_indent_refs"):
                for val in part.get(key, []) or []:
                    if val and val not in row[key]:
                        row[key].append(val)

    return list(by_indent.values())


def scrape_indent(
    *,
    headless: bool = True,
    workers: int = 5,
    wait_seconds: int = 60,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    run_id = str(int(time.time()))
    data_dir = config.RUNS_DIR / run_id
    data_dir.mkdir(parents=True, exist_ok=True)

    ensure_credentials(config.LOGIN_USER, config.LOGIN_PASSWORD)
    run_collection(
        output_dir=data_dir,
        base_url=config.BASE_URL,
        user=config.LOGIN_USER,
        password=config.LOGIN_PASSWORD,
        headful=not headless,
        wait_seconds=wait_seconds,
        auto_paginate=True,
        page_size=page_size,
    )

    perm_path = data_dir / "permutation2.txt"
    if perm_path.is_file():
        perm2 = perm_path.read_text(encoding="utf-8").strip()
        if perm2:
            build_detail_payloads_from_catalog(data_dir, perm2)
    else:
        log.warning("No permutation2 captured; detail payload auto-build skipped.")

    replay_catalog(data_dir)
    docs = _parse_detail_docs(data_dir, workers=workers)

    parsed_paths = sorted((data_dir / "parsed").glob("*.json"))
    bundle = aggregate_indent_parts(parsed_paths)
    (data_dir / "output.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    catalog = _catalog_from_docs(docs, run_id=run_id, data_dir=data_dir)
    save_json(config.INDENT_CATALOG_FILE, catalog)
    config.LAST_RUN_FILE.write_text(str(data_dir), encoding="utf-8")

    log.info("Indent scrape complete: %d record(s) → %s", len(catalog), config.INDENT_CATALOG_FILE)
    return catalog
