"""Logging, JSON I/O, JSONL I/O, failed-record tracking (vendor-bills style)."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

_failed_lock = threading.Lock()
_jsonl_lock = threading.Lock()


def setup_logging(name: str = "constructionary_v2") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    )
    logger.addHandler(console)
    return logger


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_failed_image(path: Path, record_key: str, url: str, error: str) -> None:
    with _failed_lock:
        data = load_json(path)
        if not isinstance(data, list):
            data = []
        data.append(
            {
                "record_key": record_key,
                "url": url,
                "error": error,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        save_json(path, data)


def append_failed_detail(path: Path, display_name: str, error: str) -> None:
    with _failed_lock:
        data = load_json(path)
        if not isinstance(data, list):
            data = []
        data.append(
            {
                "display_name": display_name,
                "error": error,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        save_json(path, data)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Thread-safe append of one JSON record as a line to a .jsonl file."""
    with _jsonl_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load all records from a .jsonl file; silently skip malformed lines."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def display_name_key(display_name: str) -> str:
    """Stable short key derived from display_name — used as file stem and record key."""
    import hashlib
    return hashlib.sha1(display_name.encode()).hexdigest()[:16]
