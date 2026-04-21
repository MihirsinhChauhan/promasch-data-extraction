import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from tqdm import tqdm


def setup_logging(name: str = "vendor_bills") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    ))

    file_handler = logging.FileHandler(
        Path(__file__).parent / "data" / "pipeline.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def load_json(path: Path) -> Any:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


_failed_lock = threading.Lock()


def load_failed_ids(path: Path) -> dict:
    """Returns {"po": [...], "wo": [...]}"""
    data = load_json(path)
    if isinstance(data, dict):
        return data
    return {"po": [], "wo": []}


def save_failed_id(path: Path, order_type: str, bill_id: int, error: str) -> None:
    with _failed_lock:
        failed = load_failed_ids(path)
        key = order_type.lower()
        if key not in failed:
            failed[key] = []

        existing_ids = {entry["bill_id"] for entry in failed[key]}
        if bill_id not in existing_ids:
            failed[key].append({
                "bill_id": bill_id,
                "error": error,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            save_json(path, failed)


def get_failed_bill_ids(path: Path, order_type: str) -> list[int]:
    """Return bill_ids from failed_ids.json for the given order type."""
    failed = load_failed_ids(path)
    return [entry["bill_id"] for entry in failed.get(order_type.lower(), [])]


def clear_failed_ids(path: Path, order_type: str, bill_ids: list[int]) -> None:
    """Remove successfully retried IDs from failed_ids.json."""
    with _failed_lock:
        failed = load_failed_ids(path)
        key = order_type.lower()
        if key in failed:
            remove_set = set(bill_ids)
            failed[key] = [e for e in failed[key] if e["bill_id"] not in remove_set]
            save_json(path, failed)


def get_processed_ids(metadata_records: list, order_type: str) -> set:
    """Extract bill_ids that already have an s3_url from final output records."""
    return {
        r["bill_id"] for r in metadata_records
        if r.get("type", "").upper() == order_type.upper() and r.get("s3_url")
    }


def validate_pdf(content: bytes) -> tuple[bool, str]:
    """
    Validate that content is a real PDF.
    Returns (is_valid, rejection_reason).
    """
    if not content:
        return False, "Empty response"
    if content[:5] != b"%PDF-":
        if b"<html" in content[:500].lower() or b"<!doctype" in content[:500].lower():
            return False, "HTML error page returned instead of PDF"
        return False, f"Bad magic bytes: {content[:20]!r}"
    if len(content) < 100:
        return False, f"Suspiciously small PDF ({len(content)} bytes)"
    return True, ""


class ProgressTracker:
    def __init__(self, total: int, label: str = ""):
        self.total = total
        self.label = label
        self.done = 0
        self.failed = 0
        self._start = time.time()
        self.bar = tqdm(
            total=total,
            desc=label,
            unit="file",
            ncols=100,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )

    def tick(self, success: bool = True) -> None:
        if success:
            self.done += 1
        else:
            self.failed += 1
        self.bar.update(1)
        self.bar.set_postfix(ok=self.done, fail=self.failed, refresh=False)

    def close(self) -> None:
        self.bar.close()

    @property
    def elapsed(self) -> float:
        return time.time() - self._start

    @property
    def rate(self) -> float:
        completed = self.done + self.failed
        return completed / self.elapsed if self.elapsed > 0 else 0

    def summary_line(self) -> str:
        completed = self.done + self.failed
        pct = (completed / self.total * 100) if self.total else 0
        eta = (self.total - completed) / self.rate if self.rate > 0 else 0
        return (
            f"[{self.label}] {completed}/{self.total} ({pct:.1f}%) | "
            f"ok={self.done} fail={self.failed} | "
            f"{self.rate:.1f}/s | ETA {eta:.0f}s"
        )


class RunSummary:
    """Accumulates stats across the entire pipeline run and prints a final report."""

    def __init__(self):
        self._sections: list[dict] = []
        self._start = time.time()

    def add(self, label: str, total: int, success: int, failed: int) -> None:
        self._sections.append({
            "label": label,
            "total": total,
            "success": success,
            "failed": failed,
        })

    def print_report(self, logger) -> None:
        elapsed = time.time() - self._start
        logger.info("")
        logger.info("=" * 60)
        logger.info("  PIPELINE RUN SUMMARY")
        logger.info("=" * 60)
        for s in self._sections:
            status = "OK" if s["failed"] == 0 else "WARN"
            logger.info(
                "  %-25s  total=%-6d  success=%-6d  failed=%-6d  [%s]",
                s["label"], s["total"], s["success"], s["failed"], status,
            )
        total_all = sum(s["total"] for s in self._sections)
        ok_all = sum(s["success"] for s in self._sections)
        fail_all = sum(s["failed"] for s in self._sections)
        logger.info("-" * 60)
        logger.info(
            "  %-25s  total=%-6d  success=%-6d  failed=%-6d",
            "GRAND TOTAL", total_all, ok_all, fail_all,
        )
        logger.info("  Elapsed: %.1fs", elapsed)
        logger.info("=" * 60)
