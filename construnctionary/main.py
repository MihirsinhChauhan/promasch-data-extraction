"""
Orchestrate API extraction: collect → replay → parse → output.json
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from collector import ensure_credentials, run_collection
from gwt_parser import aggregate_parts, parse_dump_file
from replay import replay_payload_file

ROOT = Path(__file__).resolve().parent


def log_error(data_dir: Path, message: str) -> None:
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "errors.log").open("a", encoding="utf-8") as f:
        f.write(f"{time.time()}: {message}\n")


def do_replay(
    data_dir: Path,
    *,
    workers: int,
    erp_url: str,
) -> List[Dict[str, Any]]:
    catalog_path = data_dir / "payload_catalog.json"
    if not catalog_path.is_file():
        log_error(data_dir, f"replay skipped: missing {catalog_path}")
        return []
    entries: List[Dict[str, Any]] = json.loads(
        catalog_path.read_text(encoding="utf-8")
    )
    auth_state = data_dir / "auth_state.json"
    auth_path = auth_state if auth_state.is_file() else None
    results: List[Dict[str, Any]] = []

    def work(entry: Dict[str, Any]) -> Dict[str, Any]:
        rel_p = entry.get("payload")
        rel_d = entry.get("dump")
        if not rel_p or not rel_d:
            return {"ok": False, "error": "missing paths"}
        payload_path = data_dir / rel_p
        dump_path = data_dir / rel_d
        if not payload_path.is_file():
            return {"ok": False, "error": f"missing payload {payload_path}"}
        try:
            return replay_payload_file(
                payload_path,
                dump_path,
                auth_state_path=auth_path,
                erp_url=erp_url,
            )
        except Exception as e:
            log_error(data_dir, f"replay {payload_path}: {e}")
            return {"ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(work, e): e for e in entries}
        for fut in as_completed(futures):
            results.append(fut.result())

    summary_path = data_dir / "logs" / "replay_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def do_parse(data_dir: Path, *, workers: int) -> List[Path]:
    dumps_dir = data_dir / "dumps"
    parsed_dir = data_dir / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    dump_files = sorted(dumps_dir.glob("*.txt"))
    written: List[Path] = []

    # Build stem → [folder, category] lookup from catalog
    cat_lookup: Dict[str, list] = {}
    catalog_path = data_dir / "payload_catalog.json"
    if catalog_path.is_file():
        for entry in json.loads(catalog_path.read_text(encoding="utf-8")):
            stem = Path(entry["dump"]).stem
            folder = entry.get("folder", "")
            category = entry.get("category", "")
            cat_lookup[stem] = [x for x in [folder, category] if x]

    def work(df: Path) -> Optional[Path]:
        out = parsed_dir / f"{df.stem}.json"
        try:
            doc = parse_dump_file(df, category_path=cat_lookup.get(df.stem, []))
            out.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            return out
        except Exception as e:
            log_error(data_dir, f"parse {df}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(work, dump_files):
            if res is not None:
                written.append(res)
    return written


def do_aggregate(data_dir: Path) -> Path:
    parsed_dir = data_dir / "parsed"
    files = sorted(parsed_dir.glob("*.json"))
    bundle = aggregate_parts(files)
    out_path = data_dir / "output.json"
    out_path.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Promasch GWT API extraction pipeline")
    p.add_argument(
        "--collect",
        action="store_true",
        help="Run Playwright collector (captures getPartDetails)",
    )
    p.add_argument(
        "--replay",
        action="store_true",
        help="Replay all payloads from payload_catalog.json",
    )
    p.add_argument("--parse", action="store_true", help="Parse all dumps/*.txt")
    p.add_argument(
        "--all",
        action="store_true",
        help="collect → replay → parse → aggregate",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Run directory (contains payloads/, dumps/, etc.). Default: data/<ts> for --all/--collect",
    )
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--erp-url", default="https://gw.promasch.in/deptherp/erp")
    # Collector passthrough
    p.add_argument("--base-url", default="https://gw.promasch.in")
    p.add_argument("--user", default=__import__("os").getenv("CONSTRUCTIONARY_USER", "Vikram@greenwave.ws"))
    p.add_argument("--password", default=__import__("os").getenv("CONSTRUCTIONARY_PASSWORD", "Infosys@9009"))
    p.add_argument("--headful", action="store_true")
    p.add_argument("--max-folders", type=int, default=5000)
    p.add_argument("--scroll-rounds", type=int, default=15, help="Base scroll rounds (adaptive per category)")
    p.add_argument("--folder-start", type=int, default=0, help="Skip first N leaf categories")
    p.add_argument("--folder-limit", type=int, default=0, help="Process at most N leaf categories (0=all)")
    p.add_argument("--sel-tree", default=None)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    do_all = args.all
    run_collect = args.collect or do_all
    run_replay = args.replay or do_all
    do_parse_f = args.parse or do_all

    if not (run_collect or run_replay or do_parse_f):
        raise SystemExit(
            "Specify at least one of: --collect, --replay, --parse, --all "
            "(use --help for options)."
        )

    data_dir = args.data_dir
    if data_dir is None:
        if run_collect or do_all:
            data_dir = ROOT / "data" / str(int(time.time()))
        else:
            raise SystemExit("--data-dir is required unless --collect or --all")

    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    if run_collect:
        ensure_credentials(args.user, args.password)
        run_collection(
            output_dir=data_dir,
            base_url=args.base_url,
            user=args.user,
            password=args.password,
            headful=args.headful,
            max_folders=args.max_folders,
            scroll_rounds=args.scroll_rounds,
            tree_sel_override=args.sel_tree,
            folder_start=args.folder_start,
            folder_limit=args.folder_limit,
        )

    if run_replay:
        print(f"[main] replay → {data_dir}")
        do_replay(data_dir, workers=args.workers, erp_url=args.erp_url)

    if do_parse_f:
        print(f"[main] parse → {data_dir}")
        written = do_parse(data_dir, workers=args.workers)
        print(f"[main] parsed {len(written)} file(s)")
        if written or (data_dir / "parsed").exists():
            out = do_aggregate(data_dir)
            print(f"[main] output → {out}")


if __name__ == "__main__":
    main()
