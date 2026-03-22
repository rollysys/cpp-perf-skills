from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CampaignConfig
from .db import open_db
from .memory import (
    append_target_history,
    build_target_memory,
    target_latest_path,
    target_history_path,
    write_case_target_memory,
    write_experiment_summary,
)
from .scheduler import (
    Assignment,
    claim_next_assignment,
)
from .util import ensure_dir, utc_now, write_json


@dataclass(frozen=True)
class HookMetrics:
    median_ns: float
    p99_ns: float | None
    stable: bool
    correctness: bool


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    existed: bool
    content: bytes | None


class HookFailure(RuntimeError):
    pass


TERMINAL_STATES = {"hardware_limit", "no_more_ideas"}


class HeartbeatThread(threading.Thread):
    def __init__(
        self,
        db_path: Path,
        runtime_heartbeat_path: Path,
        campaign_id: str,
        target_id: int,
        experiment_id: int,
        worker_id: str,
        interval_seconds: int,
        stale_after_seconds: int,
    ) -> None:
        super().__init__(daemon=True)
        self.db_path = db_path
        self.runtime_heartbeat_path = runtime_heartbeat_path
        self.campaign_id = campaign_id
        self.target_id = target_id
        self.experiment_id = experiment_id
        self.worker_id = worker_id
        self.interval_seconds = interval_seconds
        self.stale_after_seconds = stale_after_seconds
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.touch()

    def touch(self) -> None:
        now = utc_now()
        payload = {
            "campaign_id": self.campaign_id,
            "target_id": self.target_id,
            "experiment_id": self.experiment_id,
            "worker_id": self.worker_id,
            "updated_at": now,
        }
        write_json(self.runtime_heartbeat_path, payload)
        with open_db(self.db_path) as connection:
            connection.execute(
                """
                UPDATE targets
                SET last_heartbeat_at = ?, lock_expires_at = ?
                WHERE id = ?
                """,
                (now, now, self.target_id),
            )
            connection.execute(
                """
                UPDATE experiments
                SET last_heartbeat_at = ?
                WHERE id = ?
                """,
                (now, self.experiment_id),
            )
            connection.commit()


def _read_hook_json(result_path: Path, stdout_text: str) -> dict[str, object]:
    if result_path.exists():
        return json.loads(result_path.read_text(encoding="utf-8"))
    stripped = stdout_text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    return {}


def _snapshot_file(path: Path) -> FileSnapshot:
    resolved = path.resolve()
    if resolved.exists():
        return FileSnapshot(path=resolved, existed=True, content=resolved.read_bytes())
    return FileSnapshot(path=resolved, existed=False, content=None)


def _restore_snapshot(snapshot: FileSnapshot) -> bool:
    if snapshot.existed:
        assert snapshot.content is not None
        current = snapshot.path.read_bytes() if snapshot.path.exists() else None
        if current == snapshot.content:
            return False
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot.path.write_bytes(snapshot.content)
        return True
    if snapshot.path.exists():
        snapshot.path.unlink()
        return True
    return False


def _extract_metrics(payload: dict[str, object]) -> HookMetrics:
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        stats = {}
    median_ns = float(payload.get("median_ns", stats.get("median")))
    p99_raw = payload.get("p99_ns", stats.get("p99"))
    p99_ns = float(p99_raw) if p99_raw is not None else None
    stable = bool(payload.get("stable", stats.get("stable", True)))
    correctness_raw = payload.get("correctness", True)
    if isinstance(correctness_raw, str):
        correctness = correctness_raw.strip().lower() in {"1", "true", "pass", "passed", "ok"}
    else:
        correctness = bool(correctness_raw)
    return HookMetrics(
        median_ns=median_ns,
        p99_ns=p99_ns,
        stable=stable,
        correctness=correctness,
    )


def _render_command(command: tuple[str, ...], context: dict[str, str]) -> list[str]:
    return [part.format(**context) for part in command]


def _run_hook(
    config: CampaignConfig,
    hook_name: str,
    command: tuple[str, ...],
    case_dir: Path,
    context: dict[str, str],
) -> dict[str, object]:
    result_path = case_dir / f"{hook_name}.json"
    stdout_path = case_dir / f"{hook_name}.stdout"
    stderr_path = case_dir / f"{hook_name}.stderr"

    env = os.environ.copy()
    env.update(
        {
            "CPP_PERF_HOOK_NAME": hook_name,
            "CPP_PERF_RESULT_PATH": str(result_path),
            "CPP_PERF_CAMPAIGN_ID": context["campaign_id"],
            "CPP_PERF_CONTROLLER_ROOT": context["controller_root"],
            "CPP_PERF_REPO_ROOT": context["repo_root"],
            "CPP_PERF_RUNTIME_ROOT": context["runtime_root"],
            "CPP_PERF_TARGET_ID": context["target_id"],
            "CPP_PERF_TARGET_PATH": context["target_path"],
            "CPP_PERF_RELATIVE_TARGET_PATH": context["relative_target_path"],
            "CPP_PERF_CASE_DIR": context["case_dir"],
            "CPP_PERF_STRATEGY": context["strategy"],
            "CPP_PERF_STRATEGY_PASS": context["strategy_pass"],
            "CPP_PERF_ATTEMPT": context["attempt"],
            "CPP_PERF_WORKER_ID": context["worker_id"],
            "CPP_PERF_EXPERIMENT_ID": context["experiment_id"],
            "CPP_PERF_TARGET_MEMORY_PATH": context["target_memory_path"],
            "CPP_PERF_TARGET_HISTORY_PATH": context["target_history_path"],
        }
    )
    process = subprocess.run(
        _render_command(command, context),
        cwd=config.repo_root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    stdout_path.write_text(process.stdout, encoding="utf-8")
    stderr_path.write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise HookFailure(
            f"{hook_name} failed with exit code {process.returncode}; see {stderr_path}"
        )
    return _read_hook_json(result_path, process.stdout)


def _create_experiment(
    connection: sqlite3.Connection,
    assignment: Assignment,
    worker_id: str,
    case_dir: Path,
) -> int:
    now = utc_now()
    cursor = connection.execute(
        """
        INSERT INTO experiments(target_id, strategy, worker_id, status, case_dir, started_at, last_heartbeat_at)
        VALUES(?, ?, ?, 'running', ?, ?, ?)
        """,
        (assignment.target_id, assignment.strategy, worker_id, str(case_dir), now, now),
    )
    connection.commit()
    return int(cursor.lastrowid)


def _normalize_terminal_state(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in TERMINAL_STATES:
        return normalized
    return None


def _terminal_state(
    optimize_payload: dict[str, object],
    benchmark_payload: dict[str, object],
) -> str | None:
    for payload in (benchmark_payload, optimize_payload):
        state = _normalize_terminal_state(payload.get("terminal_state"))
        if state is not None:
            return state
    return None


def _bool_value(payload: dict[str, object], key: str, default: bool) -> bool:
    raw = payload.get(key, default)
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "ok", "pass", "passed"}
    return bool(raw)


def _list_of_strings(payload: dict[str, object], key: str) -> list[str]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def _experiment_summary_payload(
    assignment: Assignment,
    case_dir: Path,
    experiment_id: int,
    result_status: str,
    outcome: str | None,
    baseline_payload: dict[str, object],
    optimize_payload: dict[str, object],
    benchmark_payload: dict[str, object],
    terminal_state: str | None = None,
    error_text: str | None = None,
) -> dict[str, object]:
    summary = optimize_payload.get("summary")
    notes = optimize_payload.get("notes")
    return {
        "experiment_id": experiment_id,
        "target_id": assignment.target_id,
        "target_path": assignment.target_path,
        "strategy": assignment.strategy,
        "strategy_pass": assignment.strategy_pass,
        "status": result_status,
        "outcome": outcome,
        "terminal_state": terminal_state,
        "case_dir": str(case_dir),
        "baseline_median_ns": baseline_payload.get("median_ns", baseline_payload.get("stats", {}).get("median") if isinstance(baseline_payload.get("stats"), dict) else None),
        "optimized_median_ns": benchmark_payload.get("median_ns", benchmark_payload.get("stats", {}).get("median") if isinstance(benchmark_payload.get("stats"), dict) else None),
        "speedup": None,
        "changed": _bool_value(optimize_payload, "changed", default=False),
        "rebuild": _bool_value(optimize_payload, "rebuild", default=False),
        "correctness": _bool_value(benchmark_payload if benchmark_payload else optimize_payload, "correctness", default=True),
        "files_touched": _list_of_strings(optimize_payload, "files_touched"),
        "summary": "" if summary is None else str(summary),
        "notes": "" if notes is None else str(notes),
        "error_text": error_text,
    }


def run_once(config: CampaignConfig, worker_id: str = "worker-0") -> dict[str, object]:
    with open_db(config.db_path) as connection:
        assignment = claim_next_assignment(connection, config, worker_id)
        if assignment is None:
            return {"status": "idle"}

    case_dir = ensure_dir(
        config.cases_root
        / f"{assignment.target_id:06d}_{assignment.strategy}_p{assignment.strategy_pass}_{time.time_ns()}"
    )

    with open_db(config.db_path) as connection:
        target_memory = build_target_memory(
            connection,
            config,
            assignment.target_id,
            current_strategy=assignment.strategy,
            current_attempt=assignment.attempts + 1,
            current_strategy_pass=assignment.strategy_pass,
        )
        target_memory_path = write_case_target_memory(config, assignment.target_id, case_dir, target_memory)
        experiment_id = _create_experiment(connection, assignment, worker_id, case_dir)

    heartbeat = HeartbeatThread(
        db_path=config.db_path,
        runtime_heartbeat_path=config.heartbeat_path,
        campaign_id=config.campaign_id,
        target_id=assignment.target_id,
        experiment_id=experiment_id,
        worker_id=worker_id,
        interval_seconds=config.budget.heartbeat_interval_seconds,
        stale_after_seconds=config.budget.stale_after_seconds,
    )
    heartbeat.start()

    context = {
        "campaign_id": config.campaign_id,
        "controller_root": str(Path(__file__).resolve().parents[2]),
        "repo_root": str(config.repo_root),
        "runtime_root": str(config.runtime_root),
        "target_id": str(assignment.target_id),
        "target_path": str(config.repo_root / assignment.target_path),
        "relative_target_path": assignment.target_path,
        "case_dir": str(case_dir),
        "strategy": assignment.strategy,
        "strategy_pass": str(assignment.strategy_pass),
        "attempt": str(assignment.attempts + 1),
        "worker_id": worker_id,
        "experiment_id": str(experiment_id),
        "target_memory_path": str(target_memory_path),
        "target_history_path": str(target_history_path(config, assignment.target_id)),
    }
    target_snapshot = _snapshot_file(Path(context["target_path"]))

    baseline_payload: dict[str, object] = {}
    optimize_payload: dict[str, object] = {}
    benchmark_payload: dict[str, object] = {}
    try:
        _run_hook(config, "prepare_case", config.hooks["prepare_case"], case_dir, context)
        baseline_payload = _run_hook(config, "baseline", config.hooks["baseline"], case_dir, context)
        optimize_payload = _run_hook(config, "optimize", config.hooks["optimize"], case_dir, context)
        changed = bool(optimize_payload.get("changed", True))
        if changed:
            benchmark_payload = _run_hook(config, "benchmark", config.hooks["benchmark"], case_dir, context)
            benchmark_metrics = _extract_metrics(benchmark_payload)
        else:
            benchmark_payload = {}
            benchmark_metrics = HookMetrics(
                median_ns=float("inf"),
                p99_ns=None,
                stable=False,
                correctness=False,
            )

        baseline_metrics = _extract_metrics(baseline_payload)
        notes = optimize_payload.get("notes")
        if not changed:
            outcome = "discard"
            speedup = 0.0
            experiment_status = "finished"
        elif not benchmark_metrics.correctness or not benchmark_metrics.stable:
            outcome = "discard"
            speedup = baseline_metrics.median_ns / benchmark_metrics.median_ns
            experiment_status = "finished"
        else:
            speedup = baseline_metrics.median_ns / benchmark_metrics.median_ns
            if speedup >= config.selection.keep_min_speedup:
                outcome = "keep"
            elif speedup >= config.selection.low_gain_speedup:
                outcome = "low_gain"
            else:
                outcome = "discard"
            experiment_status = "finished"
        terminal_state = _terminal_state(optimize_payload, benchmark_payload)
        restored_target_file = False
        if outcome == "discard":
            restored_target_file = _restore_snapshot(target_snapshot)

        now = utc_now()
        with open_db(config.db_path) as connection:
            target_row = connection.execute(
                "SELECT * FROM targets WHERE id = ?",
                (assignment.target_id,),
            ).fetchone()
            assert target_row is not None
            keep_count = target_row["keep_count"] + (1 if outcome == "keep" else 0)
            low_gain_streak = 0 if outcome == "keep" else target_row["low_gain_streak"] + 1
            best_speedup = max(float(target_row["best_speedup"]), speedup)
            target_status = "completed" if terminal_state is not None else "queued"

            connection.execute(
                """
                UPDATE experiments
                SET status = ?,
                    finished_at = ?,
                    last_heartbeat_at = ?,
                    baseline_median_ns = ?,
                    optimized_median_ns = ?,
                    speedup = ?,
                    outcome = ?,
                    notes = ?
                WHERE id = ?
                """,
                (
                    experiment_status,
                    now,
                    now,
                    baseline_metrics.median_ns,
                    None if not changed else benchmark_metrics.median_ns,
                    speedup,
                    outcome,
                    None if notes is None else str(notes),
                    experiment_id,
                ),
            )
            connection.execute(
                """
                UPDATE targets
                SET status = ?,
                    attempts = attempts + 1,
                    keep_count = ?,
                    low_gain_streak = ?,
                    best_speedup = ?,
                    best_case_dir = CASE WHEN ? >= best_speedup THEN ? ELSE best_case_dir END,
                    last_strategy = ?,
                    last_speedup = ?,
                    locked_by = NULL,
                    lock_expires_at = NULL,
                    last_heartbeat_at = ?,
                    last_finished_at = ?
                WHERE id = ?
                """,
                (
                    target_status,
                    keep_count,
                    low_gain_streak,
                    best_speedup,
                    speedup,
                    str(case_dir),
                    assignment.strategy,
                    speedup,
                    now,
                    now,
                    assignment.target_id,
                ),
            )
            connection.commit()

        summary_payload = _experiment_summary_payload(
            assignment=assignment,
            case_dir=case_dir,
            experiment_id=experiment_id,
            result_status=experiment_status,
            outcome=outcome,
            baseline_payload=baseline_payload,
            optimize_payload=optimize_payload,
            benchmark_payload=benchmark_payload,
            terminal_state=terminal_state,
        )
        summary_payload["speedup"] = speedup
        summary_payload["restored_target_file"] = restored_target_file
        write_experiment_summary(case_dir, summary_payload)
        append_target_history(config, assignment.target_id, summary_payload)

        with open_db(config.db_path) as connection:
            latest_memory = build_target_memory(
                connection,
                config,
                assignment.target_id,
                current_strategy=assignment.strategy,
                current_attempt=assignment.attempts + 1,
                current_strategy_pass=assignment.strategy_pass,
            )
            write_case_target_memory(config, assignment.target_id, case_dir, latest_memory)

        return {
            "status": "ok",
            "target_path": assignment.target_path,
            "strategy": assignment.strategy,
            "strategy_pass": assignment.strategy_pass,
            "outcome": outcome,
            "terminal_state": terminal_state,
            "restored_target_file": restored_target_file,
            "speedup": round(speedup, 4),
            "case_dir": str(case_dir),
        }
    except Exception as exc:
        restored_target_file = _restore_snapshot(target_snapshot)
        now = utc_now()
        with open_db(config.db_path) as connection:
            target_row = connection.execute(
                "SELECT * FROM targets WHERE id = ?",
                (assignment.target_id,),
            ).fetchone()
            assert target_row is not None
            target_status = "queued"
            connection.execute(
                """
                UPDATE experiments
                SET status = 'crash',
                    finished_at = ?,
                    last_heartbeat_at = ?,
                    outcome = 'crash',
                    error_text = ?
                WHERE id = ?
                """,
                (now, now, str(exc), experiment_id),
            )
            connection.execute(
                """
                UPDATE targets
                SET status = ?,
                    attempts = attempts + 1,
                    low_gain_streak = low_gain_streak + 1,
                    locked_by = NULL,
                    lock_expires_at = NULL,
                    last_heartbeat_at = ?,
                    last_finished_at = ?
                WHERE id = ?
                """,
                (target_status, now, now, assignment.target_id),
            )
            connection.commit()

        summary_payload = _experiment_summary_payload(
            assignment=assignment,
            case_dir=case_dir,
            experiment_id=experiment_id,
            result_status="crash",
            outcome="crash",
            baseline_payload=baseline_payload,
            optimize_payload=optimize_payload,
            benchmark_payload=benchmark_payload,
            terminal_state=None,
            error_text=str(exc),
        )
        summary_payload["restored_target_file"] = restored_target_file
        write_experiment_summary(case_dir, summary_payload)
        append_target_history(config, assignment.target_id, summary_payload)

        with open_db(config.db_path) as connection:
            latest_memory = build_target_memory(
                connection,
                config,
                assignment.target_id,
                current_strategy=assignment.strategy,
                current_attempt=assignment.attempts + 1,
                current_strategy_pass=assignment.strategy_pass,
            )
            write_case_target_memory(config, assignment.target_id, case_dir, latest_memory)
        return {
            "status": "crash",
            "target_path": assignment.target_path,
            "strategy": assignment.strategy,
            "strategy_pass": assignment.strategy_pass,
            "error": str(exc),
            "restored_target_file": restored_target_file,
            "case_dir": str(case_dir),
        }
    finally:
        heartbeat.stop()
        heartbeat.touch()


def status_summary(config: CampaignConfig) -> dict[str, object]:
    with open_db(config.db_path) as connection:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM targets GROUP BY status ORDER BY status"
            ).fetchall()
        }
        top_targets = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, path, status, attempts, keep_count, best_speedup, last_strategy, last_speedup
                FROM targets
                ORDER BY best_speedup DESC, priority DESC, path ASC
                LIMIT 10
                """
            ).fetchall()
        ]
    for row in top_targets:
        latest_path = target_latest_path(config, int(row["id"]))
        if latest_path.exists():
            payload = json.loads(latest_path.read_text(encoding="utf-8"))
            guidance = payload.get("guidance", {})
            if isinstance(guidance, dict):
                row["next_candidates"] = guidance.get("follow_up_candidates", [])
        row.pop("id", None)
    return {"counts": counts, "top_targets": top_targets}
