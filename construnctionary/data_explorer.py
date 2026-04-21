from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def collect_parsed_parts(parsed_dir: Path) -> tuple[int, list[str], Counter[str]]:
    total_parts = 0
    ids: list[str] = []
    per_file_counts: Counter[str] = Counter()

    for json_file in sorted(parsed_dir.glob("*.json")):
        data = load_json(json_file)
        parts = data.get("parts", [])
        if not isinstance(parts, list):
            continue

        count = len(parts)
        total_parts += count
        per_file_counts[json_file.name] = count
        ids.extend(str(p.get("id")) for p in parts if isinstance(p, dict) and p.get("id"))

    return total_parts, ids, per_file_counts


def collect_output_parts(output_path: Path) -> tuple[int, int, list[str]]:
    data = load_json(output_path)
    output_parts = data.get("parts", [])
    declared_total = data.get("total")

    if not isinstance(output_parts, list):
        return 0, int(declared_total or 0), []

    ids = [str(p.get("id")) for p in output_parts if isinstance(p, dict) and p.get("id")]
    return len(output_parts), int(declared_total or 0), ids


def print_dupes(label: str, ids: list[str]) -> None:
    dupes = [(part_id, c) for part_id, c in Counter(ids).items() if c > 1]
    print(f"{label} unique ids: {len(set(ids))}")
    print(f"{label} duplicate ids: {len(dupes)}")
    if dupes:
        print(f"{label} top duplicate ids:")
        for part_id, count in sorted(dupes, key=lambda x: x[1], reverse=True)[:10]:
            print(f"  - {part_id}: {count} times")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check part count mismatch between parsed/*.json and output.json."
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("data/1775720783"),
        help="Run directory containing parsed/ and output.json (default: data/1775720783).",
    )
    args = parser.parse_args()

    run_dir = args.run_dir
    parsed_dir = run_dir / "parsed"
    output_path = run_dir / "output.json"

    if not parsed_dir.exists():
        raise SystemExit(f"parsed directory not found: {parsed_dir}")
    if not output_path.exists():
        raise SystemExit(f"output.json not found: {output_path}")

    parsed_total, parsed_ids, per_file_counts = collect_parsed_parts(parsed_dir)
    output_count, output_declared_total, output_ids = collect_output_parts(output_path)

    print("=== PART COUNT CHECK ===")
    print(f"run_dir: {run_dir}")
    print(f"parsed files: {len(per_file_counts)}")
    print(f"parsed total parts: {parsed_total}")
    print(f"output.json parts length: {output_count}")
    print(f"output.json declared total: {output_declared_total}")
    print()

    print("=== DIFF ===")
    print(f"parsed_total - output_parts_len: {parsed_total - output_count}")
    print(f"output_declared_total - output_parts_len: {output_declared_total - output_count}")
    print(f"parsed_total - output_declared_total: {parsed_total - output_declared_total}")
    print()

    print("=== ID QUALITY ===")
    print_dupes("parsed", parsed_ids)
    print_dupes("output", output_ids)


if __name__ == "__main__":
    main()
