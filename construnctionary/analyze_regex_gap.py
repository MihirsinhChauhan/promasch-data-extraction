"""
Analyze how many new unique parts the broader detection regex would recover
from existing dump files, compared to what's already been extracted.
"""

import json
import re
import time
from pathlib import Path

from gwt_parser import normalize_gwt_response, split_primitive_stream_and_table

STRICT_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")
BROAD_RE = re.compile(r"^[^(]+\(.*\).*\.\d+$")

DATA_DIR = Path("data")

# Print every N dump files during the long parse phase (balance noise vs feedback).
PARSE_PROGRESS_EVERY = 25


def _log(msg: str, *, end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def collect_dump_files(dump_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for dumps_dir in dump_dirs:
        if not dumps_dir.is_dir():
            continue
        files.extend(sorted(dumps_dir.glob("*.txt")))
    return files


def scan_string_tables(
    dump_files: list[Path],
    *,
    progress_every: int = PARSE_PROGRESS_EVERY,
) -> tuple[set[str], set[str], int, int]:
    """Scan all dump files, return sets of display names matching each regex."""
    strict_names: set[str] = set()
    broad_names: set[str] = set()
    errors = 0
    total = len(dump_files)
    t0 = time.perf_counter()

    for i, df in enumerate(dump_files, 1):
        if i == 1 or i % progress_every == 0 or i == total:
            elapsed = time.perf_counter() - t0
            pct = 100.0 * i / total if total else 100.0
            rel = f"{df.parent.parent.name}/{df.name}"
            _log(
                f"  [wip] dumps {i}/{total} ({pct:.1f}%) — {rel}  "
                f"(strict {len(strict_names):,} · broad {len(broad_names):,} · err {errors})  "
                f"[{elapsed:.0f}s]"
            )

        try:
            raw = df.read_text(encoding="utf-8", errors="replace")
            data = normalize_gwt_response(raw)
            _, st, _ = split_primitive_stream_and_table(data)

            for s in st:
                s_stripped = s.strip()
                if STRICT_RE.match(s_stripped):
                    strict_names.add(s_stripped)
                if BROAD_RE.match(s_stripped):
                    broad_names.add(s_stripped)
        except Exception:
            errors += 1

    return strict_names, broad_names, total, errors


def load_existing_display_names() -> set[str]:
    """Load display_name values from all existing output.json files."""
    existing: set[str] = set()
    paths: list[Path] = []

    for batch in sorted(DATA_DIR.glob("batch_*")):
        out = batch / "output.json"
        if out.is_file():
            paths.append(out)

    gap_out = DATA_DIR / "gap_fill" / "output.json"
    if gap_out.is_file():
        paths.append(gap_out)

    for j, out in enumerate(paths, 1):
        _log(f"  [wip] loading extracted names {j}/{len(paths)} — {out.relative_to(DATA_DIR)}")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
            n_before = len(existing)
            for part in data.get("parts", []):
                dn = part.get("display_name", "")
                if dn:
                    existing.add(dn.strip())
            added = len(existing) - n_before
            _log(f"        +{added:,} new unique display_name (running total {len(existing):,})")
        except Exception as e:
            _log(f"        skip (error): {e}")

    return existing


def main() -> None:
    dump_dirs: list[Path] = []
    for batch in sorted(DATA_DIR.glob("batch_*")):
        d = batch / "dumps"
        if d.is_dir():
            dump_dirs.append(d)
    gap_dumps = DATA_DIR / "gap_fill" / "dumps"
    if gap_dumps.is_dir():
        dump_dirs.append(gap_dumps)

    _log("=== analyze_regex_gap — regex recovery estimate ===\n")

    _log("[1/3] Collecting dump paths under data/ …")
    dump_files = collect_dump_files(dump_dirs)
    _log(f"      Found {len(dump_files):,} .txt dumps in {len(dump_dirs)} directories.\n")

    if not dump_files:
        _log("No dump files found — exiting.")
        return

    _log("[2/3] Parsing GWT dumps (work in progress; may take several minutes) …")
    t_parse = time.perf_counter()
    strict_names, broad_names, total_dumps, errors = scan_string_tables(dump_files)
    _log(f"      Parse phase finished in {time.perf_counter() - t_parse:.1f}s.\n")

    _log(f"Dump files scanned:        {total_dumps}")
    _log(f"Parse errors:              {errors}")
    _log("\n--- String table matches (unique display_name) ---")
    _log(f"Strict regex matches:      {len(strict_names):,}")
    _log(f"Broad regex matches:       {len(broad_names):,}")

    broad_only = broad_names - strict_names
    _log(f"Broad-only (missed today): {len(broad_only):,}")

    _log("\n[3/3] Loading already-extracted display_name from output.json …")
    existing = load_existing_display_names()
    _log(f"\n--- Compared to already-extracted parts ---")
    _log(f"Already extracted (unique display_name): {len(existing):,}")

    new_from_strict = strict_names - existing
    new_from_broad_only = broad_only - existing
    _log(f"Strict matches NOT in extracted:         {len(new_from_strict):,}")
    _log(f"Broad-only matches NOT in extracted:     {len(new_from_broad_only):,}")
    _log(
        f"Total new unique parts recoverable:      {len(new_from_strict) + len(new_from_broad_only):,}"
    )

    if broad_only:
        _log(f"\n--- Sample broad-only display names (first 20) ---")
        for name in sorted(broad_only)[:20]:
            _log(f"  {name}")

    if new_from_broad_only:
        _log(f"\n--- Sample NEW broad-only (not yet extracted, first 20) ---")
        for name in sorted(new_from_broad_only)[:20]:
            _log(f"  {name}")

    _log("\n=== done ===")


if __name__ == "__main__":
    main()
