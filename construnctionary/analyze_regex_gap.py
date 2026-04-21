"""
Analyze how many new unique parts the broader detection regex would recover
from existing dump files, compared to what's already been extracted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

try:
    import orjson
except ImportError:
    orjson = None

from gwt_parser import normalize_gwt_response, split_primitive_stream_and_table

STRICT_RE = re.compile(r"^([^(]+)\((.+)\)\.(\d+)$")
BROAD_RE = re.compile(r"^[^(]+\(.*\).*\.\d+$")

DATA_DIR = Path("data")

# Print every N completed dumps during the parse phase.
PARSE_PROGRESS_EVERY = 25


def _log(msg: str, *, end: str = "\n") -> None:
    print(msg, end=end, flush=True)


def _default_jobs() -> int:
    n = os.cpu_count() or 4
    return max(1, min(32, n))


def _cache_file(path: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:48]
    return cache_dir / f"{digest}.pkl"


def _file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return mtime_ns, st.st_size


def _try_load_cache(
    path: Path, cache_dir: Path, mtime_ns: int, size: int
) -> tuple[frozenset[str], frozenset[str]] | None:
    cf = _cache_file(path, cache_dir)
    if not cf.is_file():
        return None
    try:
        with open(cf, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if payload.get("mtime_ns") != mtime_ns or payload.get("size") != size:
        return None
    return frozenset(payload["strict"]), frozenset(payload["broad"])


def _write_cache(
    path: Path,
    cache_dir: Path,
    mtime_ns: int,
    size: int,
    strict: set[str],
    broad: set[str],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cf = _cache_file(path, cache_dir)
    payload = {
        "mtime_ns": mtime_ns,
        "size": size,
        "strict": list(strict),
        "broad": list(broad),
    }
    tmp = cf.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cf)


def _scan_one(
    path: Path,
    cache_dir: Path | None,
    use_cache: bool,
) -> tuple[frozenset[str], frozenset[str], bool]:
    """Returns (strict_names, broad_names, is_error)."""
    try:
        mtime_ns, size = _file_identity(path)
        if use_cache and cache_dir is not None:
            hit = _try_load_cache(path, cache_dir, mtime_ns, size)
            if hit is not None:
                return hit[0], hit[1], False

        raw = path.read_text(encoding="utf-8", errors="replace")
        data = normalize_gwt_response(raw)
        _, st, _ = split_primitive_stream_and_table(data)

        strict: set[str] = set()
        broad: set[str] = set()
        for s in st:
            s_stripped = s.strip()
            if STRICT_RE.match(s_stripped):
                strict.add(s_stripped)
            if BROAD_RE.match(s_stripped):
                broad.add(s_stripped)

        if use_cache and cache_dir is not None:
            _write_cache(path, cache_dir, mtime_ns, size, strict, broad)

        return frozenset(strict), frozenset(broad), False
    except Exception:
        return frozenset(), frozenset(), True


def _mp_worker(task: tuple[str, str, bool]) -> tuple[str, frozenset[str], frozenset[str], bool]:
    """task = (path_str, cache_dir_str or '', use_cache)."""
    path_str, cache_dir_str, use_cache = task
    path = Path(path_str)
    cache_dir = Path(cache_dir_str) if cache_dir_str else None
    strict, broad, err = _scan_one(path, cache_dir, use_cache)
    return path_str, strict, broad, err


def collect_dump_files(dump_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for dumps_dir in dump_dirs:
        if not dumps_dir.is_dir():
            continue
        files.extend(sorted(dumps_dir.glob("*.txt")))
    return files


def scan_string_tables_parallel(
    dump_files: list[Path],
    *,
    jobs: int,
    cache_dir: Path | None,
    use_cache: bool,
    progress_every: int = PARSE_PROGRESS_EVERY,
) -> tuple[set[str], set[str], int, int]:
    strict_names: set[str] = set()
    broad_names: set[str] = set()
    errors = 0
    total = len(dump_files)
    t0 = time.perf_counter()

    cache_dir_str = str(cache_dir.resolve()) if cache_dir else ""
    tasks: list[tuple[str, str, bool]] = [
        (str(p.resolve()), cache_dir_str, use_cache) for p in dump_files
    ]

    completed = 0
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futures = [ex.submit(_mp_worker, t) for t in tasks]
        for fut in as_completed(futures):
            completed += 1
            _path_str, strict_f, broad_f, err = fut.result()
            if err:
                errors += 1
            else:
                strict_names.update(strict_f)
                broad_names.update(broad_f)

            if completed == 1 or completed % progress_every == 0 or completed == total:
                elapsed = time.perf_counter() - t0
                pct = 100.0 * completed / total if total else 100.0
                _log(
                    f"  [wip] dumps {completed}/{total} ({pct:.1f}%)  "
                    f"(strict {len(strict_names):,} · broad {len(broad_names):,} · err {errors})  "
                    f"[{elapsed:.0f}s]"
                )

    return strict_names, broad_names, total, errors


def scan_string_tables_sequential(
    dump_files: list[Path],
    *,
    cache_dir: Path | None,
    use_cache: bool,
    progress_every: int = PARSE_PROGRESS_EVERY,
) -> tuple[set[str], set[str], int, int]:
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

        strict_f, broad_f, err = _scan_one(df, cache_dir, use_cache)
        if err:
            errors += 1
        else:
            strict_names.update(strict_f)
            broad_names.update(broad_f)

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
            raw = out.read_bytes()
            if orjson is not None:
                data = orjson.loads(raw)
            else:
                data = json.loads(raw.decode("utf-8"))
            n_before = len(existing)
            for part in data.get("parts", []):
                dn = part.get("display_name", "")
                if dn:
                    existing.add(str(dn).strip())
            added = len(existing) - n_before
            _log(f"        +{added:,} new unique display_name (running total {len(existing):,})")
        except Exception as e:
            _log(f"        skip (error): {e}")

    return existing


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Estimate regex-gap recovery from existing GWT dumps (parallel + cache)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N dump files (after sort).",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help=f"Parallel worker processes (default: {_default_jobs()}). Use 1 for single-threaded.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=f"Per-dump parse cache directory (default: {DATA_DIR / '.regex_gap_cache'}).",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable read/write of the parse cache.",
    )
    args = parser.parse_args()

    jobs = args.jobs if args.jobs is not None else _default_jobs()
    if jobs < 1:
        jobs = os.cpu_count() or 1

    use_cache = not args.no_cache
    cache_dir: Path | None = None
    if use_cache:
        cache_dir = args.cache_dir if args.cache_dir is not None else (DATA_DIR / ".regex_gap_cache")

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
    if args.limit is not None:
        dump_files = dump_files[: max(0, args.limit)]
    _log(f"      Found {len(dump_files):,} .txt dumps in {len(dump_dirs)} directories.")
    if use_cache and cache_dir is not None:
        _log(f"      Parse cache: {cache_dir.resolve()} (use --no-cache to disable)")
    _log(f"      Workers: {jobs}\n")

    if not dump_files:
        _log("No dump files found — exiting.")
        return

    _log("[2/3] Parsing GWT dumps (work in progress) …")
    if orjson is not None:
        _log("      output.json loader: orjson")
    else:
        _log("      output.json loader: stdlib json (install orjson for faster [3/3])")

    t_parse = time.perf_counter()
    if jobs == 1:
        strict_names, broad_names, total_dumps, errors = scan_string_tables_sequential(
            dump_files, cache_dir=cache_dir, use_cache=use_cache
        )
    else:
        strict_names, broad_names, total_dumps, errors = scan_string_tables_parallel(
            dump_files,
            jobs=jobs,
            cache_dir=cache_dir,
            use_cache=use_cache,
        )
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
