#!/usr/bin/env python3
"""
Constructionary v2 Extraction Pipeline
=======================================

Phases:
  enumerate  — Playwright login + tree walk; intercepts bulk getPartDetails
               RPCs and writes parts_index.jsonl.  Also captures the
               getStockForStockroom RPC template.
  detail     — Thread-pool worker: calls getStockForStockroom for every
               entry in parts_index.jsonl; dumps raw responses.
  parse      — Re-parses all detail dumps → parts_records.jsonl.
  images     — Downloads images (session cookies) + uploads to S3; updates
               parts_records.jsonl with image_s3_urls.
  import     — Loads parts_records.jsonl into Postgres table
               constructionary_v2_parts.
  all        — Runs enumerate → detail → parse → images → import in sequence.

Usage examples:
  python main.py --phase enumerate --headful
  python main.py --phase enumerate --limit 10 --headful
  python main.py --phase detail --workers 3
  python main.py --phase detail --limit 100
  python main.py --phase parse
  python main.py --phase parse --resume
  python main.py --phase images --dry-run
  python main.py --phase import --create-table --upsert
  python main.py --phase all --limit 100 --dry-run
  python main.py --phase all --resume
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import config
from utils import setup_logging

log = setup_logging("main")


@dataclass
class PhaseProgress:
    phase: str
    index: int
    total: int
    started_at: float
    ended_at: float | None = None
    status: str = "pending"  # pending | running | success | failed | interrupted
    result: object | None = None
    error: str | None = None

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return max(0.0, time.time() - self.started_at)
        return max(0.0, self.ended_at - self.started_at)


def _phase_badge(status: str) -> str:
    if status == "success":
        return "[OK]"
    if status == "failed":
        return "[FAIL]"
    if status == "interrupted":
        return "[STOP]"
    if status == "running":
        return "[RUN]"
    return "[...]"


def _log_progress_snapshot(progress_entries: list[PhaseProgress]) -> None:
    log.info("Progress:")
    for p in progress_entries:
        msg = f"  {_phase_badge(p.status)} {p.index}/{p.total} {p.phase}"
        if p.status in {"success", "failed", "interrupted"}:
            msg += f" ({p.duration_s:.1f}s)"
        log.info(msg)


def _log_final_progress_summary(progress_entries: list[PhaseProgress]) -> None:
    log.info("=" * 60)
    log.info("Progress Summary")
    for p in progress_entries:
        line = (
            f"  {_phase_badge(p.status)} {p.index}/{p.total} {p.phase} "
            f"| {p.duration_s:.1f}s"
        )
        if p.status == "success":
            line += f" | result={p.result}"
        elif p.error:
            line += f" | error={p.error}"
        log.info(line)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Phase helpers
# ---------------------------------------------------------------------------

def phase_enumerate(args: argparse.Namespace, data_dir: Path) -> int:
    from collector import run_collection
    return run_collection(
        data_dir=data_dir,
        base_url=args.base_url,
        user=args.user or config.PROM_USER,
        password=args.password or config.PROM_PASSWORD,
        headful=args.headful,
        max_folders=args.max_folders,
        scroll_rounds=args.scroll_rounds,
        tree_sel_override=args.sel_tree,
        folder_start=args.folder_start,
        folder_limit=args.folder_limit,
        limit=args.limit,
        resume=args.resume,
    )


def phase_detail(args: argparse.Namespace, data_dir: Path) -> int:
    from detail_client import run_detail_client
    return run_detail_client(
        data_dir=data_dir,
        workers=args.workers,
        delay=args.delay,
        limit=args.limit,
        resume=args.resume,
    )


def phase_parse(args: argparse.Namespace, data_dir: Path) -> int:
    from detail_parser import run_parse
    return run_parse(
        data_dir=data_dir,
        limit=args.limit,
        resume=args.resume,
    )


def phase_images(args: argparse.Namespace, data_dir: Path) -> int:
    from image_pipeline import run_image_pipeline
    return run_image_pipeline(
        data_dir=data_dir,
        workers=args.workers,
        limit=args.limit,
        dry_run=args.dry_run,
        resume=args.resume,
    )


def phase_import(args: argparse.Namespace, data_dir: Path) -> int:
    from import_postgres import run_import
    return run_import(
        data_dir=data_dir,
        dsn=args.dsn,
        create_table=args.create_table,
        recreate_table=args.recreate_table,
        truncate=args.truncate,
        upsert=args.upsert,
        batch_size=args.batch_size,
        limit=args.limit,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Constructionary v2 Extraction Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Phase selection ────────────────────────────────────────────────────
    p.add_argument(
        "--phase",
        choices=["enumerate", "detail", "parse", "images", "import", "all"],
        default="all",
        help="Which phase to run (default: all)",
    )

    # ── Data directory ─────────────────────────────────────────────────────
    p.add_argument(
        "--data-dir",
        type=Path,
        default=config.DATA_DIR,
        metavar="DIR",
        help=f"Root data directory (default: {config.DATA_DIR})",
    )

    # ── Enumerate phase options ────────────────────────────────────────────
    p.add_argument("--base-url", default=config.PROM_BASE_URL, metavar="URL")
    p.add_argument("--user", default="", metavar="EMAIL",
                   help="Promasch login email (default: CONSTRUCTIONARY_USER env)")
    p.add_argument("--password", default="", metavar="PASS",
                   help="Promasch login password (default: CONSTRUCTIONARY_PASSWORD env)")
    p.add_argument("--headful", action="store_true",
                   help="Show the browser during enumerate phase (useful for template capture)")
    p.add_argument("--max-folders", type=int, default=5000, metavar="N")
    p.add_argument("--scroll-rounds", type=int, default=15, metavar="N",
                   help="Base scroll rounds per leaf category (default 15; adapted automatically)")
    p.add_argument("--folder-start", type=int, default=0, metavar="N",
                   help="Skip first N leaf categories")
    p.add_argument("--folder-limit", type=int, default=0, metavar="N",
                   help="Stop after N leaf categories (0 = unlimited)")
    p.add_argument("--sel-tree", default=None, metavar="CSS",
                   help="Override tree CSS selector (auto-detected by default)")

    # ── Concurrency / rate ─────────────────────────────────────────────────
    p.add_argument(
        "--workers",
        type=int,
        default=config.MAX_WORKERS,
        metavar="N",
        help=f"Parallel workers for detail/image phases (default: {config.MAX_WORKERS})",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=config.REQUEST_DELAY,
        metavar="SEC",
        help=f"Inter-request delay for detail phase (default: {config.REQUEST_DELAY}s)",
    )

    # ── Common options ─────────────────────────────────────────────────────
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N parts (sample mode)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip already-completed items (idempotent re-run)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip S3 uploads and DB writes (download + parse only)",
    )

    # ── Import-phase options ───────────────────────────────────────────────
    p.add_argument("--dsn", default=None, metavar="URI",
                   help="Postgres connection URI (overrides DATABASE_URL / PG* env)")
    p.add_argument("--create-table", action="store_true",
                   help="CREATE TABLE + indexes before import")
    p.add_argument("--recreate-table", action="store_true",
                   help="DROP + CREATE table (implies --create-table)")
    p.add_argument("--truncate", action="store_true",
                   help="DELETE all rows before import")
    p.add_argument("--upsert", action="store_true",
                   help="ON CONFLICT (entity_id) DO UPDATE")
    p.add_argument("--batch-size", type=int, default=500, metavar="N",
                   help="DB insert batch size (default 500)")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    data_dir: Path = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    _PHASES = ["enumerate", "detail", "parse", "images", "import"]
    run_phases = _PHASES if args.phase == "all" else [args.phase]

    log.info("=" * 60)
    log.info("Constructionary v2 Pipeline")
    log.info("Phase(s): %s", ", ".join(run_phases))
    log.info("Data dir: %s", data_dir)
    log.info("Limit: %s | Resume: %s | Dry-run: %s", args.limit or "none", args.resume, args.dry_run)
    log.info("Workers: %d | Delay: %.2fs", args.workers, args.delay)
    log.info("=" * 60)

    # Validate credentials early for enumerate phase
    if "enumerate" in run_phases:
        user = args.user or config.PROM_USER
        password = args.password or config.PROM_PASSWORD
        if not user or not password:
            log.error(
                "Missing Promasch credentials. "
                "Pass --user / --password or set CONSTRUCTIONARY_USER / CONSTRUCTIONARY_PASSWORD."
            )
            return 1

    # Validate AWS creds for images phase (unless dry-run)
    if "images" in run_phases and not args.dry_run:
        if not config.AWS_ACCESS_KEY_ID or not config.AWS_SECRET_ACCESS_KEY:
            log.error("AWS credentials not set. Check .env file. (Use --dry-run to skip S3.)")
            return 1

    phase_funcs = {
        "enumerate": phase_enumerate,
        "detail": phase_detail,
        "parse": phase_parse,
        "images": phase_images,
        "import": phase_import,
    }

    progress_entries: list[PhaseProgress] = []
    total_phases = len(run_phases)

    for idx, phase in enumerate(run_phases, start=1):
        phase_progress = PhaseProgress(
            phase=phase,
            index=idx,
            total=total_phases,
            started_at=time.time(),
            status="running",
        )
        progress_entries.append(phase_progress)

        log.info("-" * 40)
        log.info("PHASE %d/%d: %s", idx, total_phases, phase.upper())
        log.info("-" * 40)
        _log_progress_snapshot(progress_entries)
        try:
            result = phase_funcs[phase](args, data_dir)
            phase_progress.status = "success"
            phase_progress.result = result
            phase_progress.ended_at = time.time()
            log.info("Phase %s complete: %s (%.1fs)", phase, result, phase_progress.duration_s)
        except KeyboardInterrupt:
            phase_progress.status = "interrupted"
            phase_progress.error = "keyboard interrupt"
            phase_progress.ended_at = time.time()
            log.warning("Interrupted during phase %s", phase)
            _log_final_progress_summary(progress_entries)
            return 130
        except Exception as e:
            phase_progress.status = "failed"
            phase_progress.error = str(e)
            phase_progress.ended_at = time.time()
            log.error("Phase %s FAILED: %s", phase, e)
            if args.phase == "all":
                log.error("Stopping pipeline. Fix the error and re-run with --resume.")
            _log_final_progress_summary(progress_entries)
            return 1

    _log_final_progress_summary(progress_entries)
    log.info("=" * 60)
    log.info("Pipeline complete.")
    log.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
