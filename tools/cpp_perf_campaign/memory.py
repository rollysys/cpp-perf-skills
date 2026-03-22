from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import CampaignConfig
from .scheduler import ordered_strategies, strategy_attempt_states
from .util import ensure_dir, write_json


def experiment_summary_path(case_dir: Path) -> Path:
    return case_dir / "experiment.summary.json"


def target_state_root(config: CampaignConfig) -> Path:
    return ensure_dir(config.runtime_root / "targets")


def target_latest_path(config: CampaignConfig, target_id: int) -> Path:
    return target_state_root(config) / f"{target_id:06d}.latest.json"


def target_history_path(config: CampaignConfig, target_id: int) -> Path:
    return target_state_root(config) / f"{target_id:06d}.history.jsonl"


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _read_case_summary(case_dir: Path) -> dict[str, object]:
    summary_path = experiment_summary_path(case_dir)
    if not summary_path.exists():
        return {}
    return _read_json(summary_path)


def _best_result(target_row: sqlite3.Row) -> dict[str, object] | None:
    best_case_dir = target_row["best_case_dir"]
    if not best_case_dir:
        return None
    summary = _read_case_summary(Path(str(best_case_dir)))
    if not summary:
        return None
    return summary


def build_target_memory(
    connection: sqlite3.Connection,
    config: CampaignConfig,
    target_id: int,
    current_strategy: str,
    current_attempt: int,
    current_strategy_pass: int = 1,
    history_limit: int = 8,
) -> dict[str, object]:
    target_row = connection.execute(
        "SELECT * FROM targets WHERE id = ?",
        (target_id,),
    ).fetchone()
    if target_row is None:
        raise ValueError(f"Unknown target id {target_id}")

    strategy_index: dict[str, dict[str, object]] = {}
    for strategy in config.strategies:
        strategy_index[strategy.name] = {
            "name": strategy.name,
            "kind": strategy.kind,
            "weight": strategy.weight,
            "max_passes": strategy.max_passes,
            "attempts": 0,
            "remaining_passes": strategy.max_passes,
            "next_pass": 1,
            "eligible_for_refine": False,
            "best_speedup": None,
            "last_outcome": None,
            "last_summary": None,
            "last_notes": None,
            "last_changed": None,
        }

    recent_rows = connection.execute(
        """
        SELECT id, strategy, status, case_dir, started_at, finished_at,
               baseline_median_ns, optimized_median_ns, speedup, outcome, notes, error_text
        FROM experiments
        WHERE target_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (target_id, history_limit),
    ).fetchall()

    recent_experiments: list[dict[str, object]] = []
    avoid_repeating: list[dict[str, object]] = []
    successful_directions: list[dict[str, object]] = []

    for row in recent_rows:
        case_summary = _read_case_summary(Path(row["case_dir"]))
        history_item = {
            "experiment_id": row["id"],
            "strategy": row["strategy"],
            "status": row["status"],
            "outcome": row["outcome"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "case_dir": row["case_dir"],
            "baseline_median_ns": row["baseline_median_ns"],
            "optimized_median_ns": row["optimized_median_ns"],
            "speedup": row["speedup"],
            "notes": row["notes"],
            "error_text": row["error_text"],
            "strategy_pass": case_summary.get("strategy_pass"),
            "terminal_state": case_summary.get("terminal_state"),
            "changed": case_summary.get("changed"),
            "rebuild": case_summary.get("rebuild"),
            "correctness": case_summary.get("correctness"),
            "files_touched": case_summary.get("files_touched", []),
            "summary": case_summary.get("summary"),
        }
        recent_experiments.append(history_item)

        strategy_name = str(row["strategy"])
        state = strategy_index.get(strategy_name)
        if state is not None:
            state["attempts"] = int(state["attempts"]) + 1
            if state["last_outcome"] is None:
                state["last_outcome"] = row["outcome"]
                state["last_summary"] = history_item["summary"]
                state["last_notes"] = row["notes"]
                state["last_changed"] = case_summary.get("changed")
            speedup = row["speedup"]
            if speedup is not None:
                current_best = state["best_speedup"]
                if current_best is None or float(speedup) > float(current_best):
                    state["best_speedup"] = float(speedup)

        if row["error_text"]:
            avoid_repeating.append(
                {
                    "strategy": strategy_name,
                    "reason": str(row["error_text"]),
                }
            )
        elif case_summary.get("changed") is False and row["notes"]:
            avoid_repeating.append(
                {
                    "strategy": strategy_name,
                    "reason": str(row["notes"]),
                }
            )

        if row["outcome"] in {"keep", "low_gain"}:
            successful_directions.append(
                {
                    "strategy": strategy_name,
                    "strategy_pass": case_summary.get("strategy_pass"),
                    "speedup": row["speedup"],
                    "summary": history_item["summary"],
                }
            )

    strategy_states = strategy_attempt_states(connection, target_id)
    remaining = ordered_strategies(config, int(target_row["keep_count"]), strategy_states)
    remaining_payload = [
        {
            "name": option.name,
            "pass": option.next_pass,
            "kind": option.kind,
            "refine": option.refine,
            "forced": option.forced,
            "best_speedup": option.best_speedup,
        }
        for option in remaining
    ]

    for strategy in config.strategies:
        state = strategy_index[strategy.name]
        attempts = int(state["attempts"])
        state["remaining_passes"] = max(0, strategy.max_passes - attempts)
        state["next_pass"] = attempts + 1 if attempts < strategy.max_passes else None
        state["eligible_for_refine"] = (
            attempts > 0
            and attempts < strategy.max_passes
            and state["last_outcome"] in {"keep", "low_gain"}
        )

    return {
        "target": {
            "id": target_row["id"],
            "path": target_row["path"],
            "shard": target_row["shard"],
            "priority": target_row["priority"],
            "status": target_row["status"],
            "attempts": target_row["attempts"],
            "keep_count": target_row["keep_count"],
            "low_gain_streak": target_row["low_gain_streak"],
            "best_speedup": target_row["best_speedup"],
            "last_strategy": target_row["last_strategy"],
            "last_speedup": target_row["last_speedup"],
            "best_case_dir": target_row["best_case_dir"],
        },
        "current_attempt": {
            "attempt_number": current_attempt,
            "strategy": current_strategy,
            "strategy_pass": current_strategy_pass,
            "remaining_strategies": remaining_payload,
        },
        "best_result": _best_result(target_row),
        "recent_experiments": recent_experiments,
        "strategy_status": [strategy_index[strategy.name] for strategy in config.strategies],
        "guidance": {
            "remaining_strategies": remaining_payload,
            "follow_up_candidates": remaining_payload[:3],
            "avoid_repeating_without_new_evidence": avoid_repeating[:5],
            "successful_directions": successful_directions[:3],
        },
    }


def write_case_target_memory(
    config: CampaignConfig,
    target_id: int,
    case_dir: Path,
    payload: dict[str, object],
) -> Path:
    path = case_dir / "target_memory.json"
    write_json(path, payload)
    write_json(target_latest_path(config, target_id), payload)
    return path


def append_target_history(
    config: CampaignConfig,
    target_id: int,
    payload: dict[str, object],
) -> Path:
    path = target_history_path(config, target_id)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return path


def write_experiment_summary(case_dir: Path, payload: dict[str, object]) -> Path:
    path = experiment_summary_path(case_dir)
    write_json(path, payload)
    return path
