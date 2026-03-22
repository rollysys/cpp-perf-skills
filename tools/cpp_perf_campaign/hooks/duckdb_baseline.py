#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .duckdb_common import emit_payload, load_manifest, run_duckdb_benchmark, write_json
except ImportError:
    from duckdb_common import emit_payload, load_manifest, run_duckdb_benchmark, write_json  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the baseline DuckDB benchmark for a prepared case.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    case_dir = Path(args.case_dir).resolve()
    manifest = load_manifest(case_dir)
    payload = run_duckdb_benchmark(
        repo_root=repo_root,
        benchmark_path=str(manifest["benchmark_path"]),
        out_path=case_dir / "baseline.timings",
        label="baseline",
        force_rebuild=args.force_rebuild,
    )
    write_json(case_dir / "baseline_stats.json", payload)
    emit_payload(payload)


if __name__ == "__main__":
    main()
