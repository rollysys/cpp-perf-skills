from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import load_config
from .db import init_db, open_db
from .discovery import discover_targets, upsert_targets
from .runner import run_once, status_summary
from .util import ensure_dir, write_json
from .watchdog import requeue_stale_runs


def _command_init(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    ensure_dir(config.runtime_root)
    write_json(config.runtime_root / "config.snapshot.json", config.snapshot_payload())
    print(f"Initialized campaign '{config.campaign_id}' at {config.runtime_root}")
    return 0


def _command_discover(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    frontier_path = Path(args.frontier_jsonl).resolve() if args.frontier_jsonl else None
    targets = discover_targets(config, frontier_path)
    with open_db(config.db_path) as connection:
        upsert_targets(connection, targets)
    print(f"Discovered {len(targets)} targets")
    return 0


def _command_run_once(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    result = run_once(config, worker_id=args.worker_id)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _command_run_loop(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    iteration = 0
    while args.max_iterations <= 0 or iteration < args.max_iterations:
        if config.stop_file.exists():
            print(f"Stop file detected at {config.stop_file}, exiting loop")
            return 0
        with open_db(config.db_path) as connection:
            reclaimed = requeue_stale_runs(connection, config)
        if reclaimed:
            print(f"Watchdog requeued {reclaimed} stale runs")
        result = run_once(config, worker_id=args.worker_id)
        print(json.dumps(result, indent=2, sort_keys=True))
        iteration += 1
        if result.get("status") == "idle":
            time.sleep(args.sleep_seconds)
    return 0


def _command_watchdog(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    with open_db(config.db_path) as connection:
        reclaimed = requeue_stale_runs(connection, config)
    print(f"Requeued {reclaimed} stale runs")
    return 0


def _command_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    init_db(config)
    summary = status_summary(config)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Continuous campaign controller for cpp-perf")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_init = subparsers.add_parser("init", help="Initialize campaign runtime state")
    parser_init.add_argument("config", help="Path to campaign JSON config")
    parser_init.set_defaults(func=_command_init)

    parser_discover = subparsers.add_parser("discover", help="Discover C++ targets in the repo")
    parser_discover.add_argument("config", help="Path to campaign JSON config")
    parser_discover.add_argument(
        "--frontier-jsonl",
        help="Optional JSONL file with target priorities, one object per line: {\"path\": ..., \"priority\": ...}",
    )
    parser_discover.set_defaults(func=_command_discover)

    parser_run_once = subparsers.add_parser("run-once", help="Run a single optimization experiment")
    parser_run_once.add_argument("config", help="Path to campaign JSON config")
    parser_run_once.add_argument("--worker-id", default="worker-0", help="Logical worker id")
    parser_run_once.set_defaults(func=_command_run_once)

    parser_run_loop = subparsers.add_parser("run-loop", help="Run experiments until stopped")
    parser_run_loop.add_argument("config", help="Path to campaign JSON config")
    parser_run_loop.add_argument("--worker-id", default="worker-0", help="Logical worker id")
    parser_run_loop.add_argument("--sleep-seconds", type=int, default=5, help="Idle sleep interval")
    parser_run_loop.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Maximum iterations before exit; 0 means run until stop file or interruption",
    )
    parser_run_loop.set_defaults(func=_command_run_loop)

    parser_watchdog = subparsers.add_parser("watchdog", help="Requeue stale running experiments")
    parser_watchdog.add_argument("config", help="Path to campaign JSON config")
    parser_watchdog.set_defaults(func=_command_watchdog)

    parser_status = subparsers.add_parser("status", help="Show campaign status summary")
    parser_status.add_argument("config", help="Path to campaign JSON config")
    parser_status.set_defaults(func=_command_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
