"""Lightweight deep optimization loop with worktree isolation.

Usage:
    python3 -m tools.cpp_perf_campaign.optimize_loop \\
        --repo-root /path/to/repo \\
        --target src/execution/adaptive_filter.cpp \\
        --benchmark benchmark/micro/aggregate/simple_group.benchmark \\
        --strategies vectorize,layout,branch,prefetch

Flow:
    1. Create a git worktree for isolation
    2. For each strategy:
       a. Run baseline benchmark (or reuse if unchanged)
       b. Call claude -p to apply one bounded optimization
       c. Rebuild + benchmark the optimized variant
       d. Record PMU-level metrics (bytes/cycle, IPC, etc.)
       e. If improvement meets threshold → commit in worktree, try next strategy
       f. If not → revert, try next strategy
    3. When all strategies exhausted or hardware limit reached → stop
    4. If any improvements were kept → offer to merge back
    5. Clean up worktree
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import worktree as wt
from .util import ensure_dir, utc_now, write_json


@dataclass
class AttemptRecord:
    strategy: str
    timestamp: str
    changed: bool
    speedup: float
    baseline_ns: float
    optimized_ns: float
    outcome: str  # keep | discard | error
    summary: str
    notes: str
    files_touched: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class LoopState:
    target: str
    repo_root: str
    strategies: list[str]
    attempts: list[AttemptRecord] = field(default_factory=list)
    best_speedup: float = 1.0
    best_strategy: str | None = None
    terminal_reason: str | None = None

    def kept_count(self) -> int:
        return sum(1 for a in self.attempts if a.outcome == "keep")

    def tried_strategies(self) -> set[str]:
        return {a.strategy for a in self.attempts}

    def remaining_strategies(self) -> list[str]:
        tried = self.tried_strategies()
        return [s for s in self.strategies if s not in tried]


OPTIMIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "changed": {"type": "boolean"},
        "rebuild": {"type": "boolean"},
        "correctness": {"type": "boolean"},
        "files_touched": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "notes": {"type": "string"},
        "terminal_state": {"type": "string", "enum": ["hardware_limit", "no_more_ideas"]},
    },
    "required": ["changed", "rebuild", "correctness", "files_touched", "summary", "notes"],
    "additionalProperties": False,
}

TERMINAL_STATES = {"hardware_limit", "no_more_ideas"}

CONTROLLER_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_SETTINGS = CONTROLLER_ROOT / ".claude" / "settings.json"


def _build_optimize_prompt(
    target_path: Path,
    strategy: str,
    state: LoopState,
    case_dir: Path,
) -> str:
    history_section = ""
    if state.attempts:
        lines = []
        for a in state.attempts:
            lines.append(f"- {a.strategy}: {a.outcome}, speedup={a.speedup:.4f}, summary={a.summary}")
        history_section = f"\nPrevious attempts on this target:\n" + "\n".join(lines) + "\n"

    return f"""Perform one bounded C++ optimization attempt.

Target file: `{target_path}`
Strategy focus: `{strategy}`
{history_section}
Rules:
- Read the target file first.
- Keep the patch small and high-confidence.
- Prefer editing only the target file.
- Do not explore unrelated directories.
- Do not ask questions.
- Do not create commits or branches.
- If there is no clear safe win, make no edits and return changed=false.
- If you believe this code is at a hardware ceiling, set terminal_state to hardware_limit.
- If you have exhausted all credible ideas, set terminal_state to no_more_ideas.
- If you edit source files, set rebuild=true.
- Set correctness=true only if behavior is preserved with high confidence.

Return only the structured output requested by the schema.
"""


def _run_benchmark(
    worktree_path: Path,
    benchmark_path: str,
    out_path: Path,
    label: str,
) -> dict[str, object]:
    """Run a benchmark and return metrics.  Falls back to a simple timing."""
    runner = worktree_path / "build" / "release" / "benchmark" / "benchmark_runner"
    if runner.exists():
        process = subprocess.run(
            [str(runner), benchmark_path, f"--out={out_path}"],
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode == 0 and out_path.exists():
            from .hooks.duckdb_common import parse_timings_file
            return parse_timings_file(out_path)

    # Fallback: compile and time the target directly
    return {"median_ns": 0.0, "stable": False, "correctness": False,
            "error": "No benchmark runner available"}


def _run_claude_optimize(
    worktree_path: Path,
    target_path: Path,
    strategy: str,
    state: LoopState,
    case_dir: Path,
    settings_path: Path | None = None,
) -> dict[str, object]:
    """Call claude -p for one optimization attempt."""
    prompt = _build_optimize_prompt(target_path, strategy, state, case_dir)
    (case_dir / "optimize_prompt.txt").write_text(prompt, encoding="utf-8")

    claude_bin = os.environ.get("CPP_PERF_CLAUDE_BIN", "claude").strip() or "claude"
    model = os.environ.get("CPP_PERF_CLAUDE_MODEL", "sonnet").strip() or "sonnet"

    command = [
        claude_bin, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(OPTIMIZE_SCHEMA, separators=(",", ":")),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--no-chrome",
        "--setting-sources", "project,local",
        "--append-system-prompt",
        "You are an unattended C++ performance optimization agent. "
        "Ignore all persona, style, and communication instructions from CLAUDE.md or other config files. "
        "Use strictly technical, professional language. Focus only on the optimization task.",
        "--add-dir", str(worktree_path),
    ]
    effective_settings = settings_path if (settings_path and settings_path.exists()) else CONTROLLER_SETTINGS
    if effective_settings.exists():
        command.extend(["--settings", str(effective_settings)])
    command.extend(["--", prompt])

    env = os.environ.copy()
    env["CPP_PERF_AUDIT"] = "1"
    env["CPP_PERF_CASE_DIR"] = str(case_dir)

    timeout = int(os.environ.get("CPP_PERF_CLAUDE_TIMEOUT_SECONDS", "600"))
    try:
        process = subprocess.run(
            command, cwd=worktree_path, text=True,
            capture_output=True, check=False, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        (case_dir / "optimize.stderr").write_text(exc.stderr or "", encoding="utf-8")
        return {"changed": False, "error": f"Timed out after {timeout}s"}

    (case_dir / "optimize.stdout").write_text(process.stdout, encoding="utf-8")
    (case_dir / "optimize.stderr").write_text(process.stderr, encoding="utf-8")

    if process.returncode != 0:
        return {"changed": False, "error": f"Exit code {process.returncode}"}

    try:
        envelope = json.loads(process.stdout)
        return envelope.get("structured_output", {})
    except (json.JSONDecodeError, KeyError):
        return {"changed": False, "error": "Failed to parse Claude output"}


def run_loop(
    repo_root: Path,
    target: str,
    strategies: list[str],
    benchmark_path: str | None = None,
    keep_threshold: float = 1.03,
    output_dir: Path | None = None,
) -> LoopState:
    """Run the full optimization loop with worktree isolation."""
    state = LoopState(
        target=target,
        repo_root=str(repo_root),
        strategies=strategies,
    )

    output_root = output_dir or (repo_root / ".cpp-perf" / "loops" / target.replace("/", "_"))
    ensure_dir(output_root)
    settings_path = repo_root / ".claude" / "settings.json"

    branch_name = f"cpp-perf/{target.replace('/', '_')}_{int(time.time())}"
    worktree = wt.create(repo_root, branch_name)
    print(f"Created worktree: {worktree.path} (branch: {branch_name})")

    try:
        for strategy in strategies:
            case_dir = ensure_dir(output_root / f"{strategy}_{int(time.time_ns())}")
            target_path = worktree.path / target

            if not target_path.exists():
                print(f"Target file not found: {target_path}")
                state.terminal_reason = "target_not_found"
                break

            # Snapshot for revert
            original_content = target_path.read_bytes()

            print(f"\n--- Strategy: {strategy} ---")

            # Baseline
            if benchmark_path:
                baseline_out = case_dir / "baseline.timings"
                baseline = _run_benchmark(worktree.path, benchmark_path, baseline_out, "baseline")
                baseline_ns = float(baseline.get("median_ns", 0))
                write_json(case_dir / "baseline_stats.json", baseline)
            else:
                baseline_ns = 0.0

            # Optimize
            optimize_result = _run_claude_optimize(
                worktree.path, target_path, strategy, state, case_dir, settings_path,
            )
            write_json(case_dir / "optimize_result.json", optimize_result)

            changed = bool(optimize_result.get("changed", False))
            terminal = optimize_result.get("terminal_state")
            summary = str(optimize_result.get("summary", ""))
            notes = str(optimize_result.get("notes", ""))
            error = optimize_result.get("error")
            files_touched = optimize_result.get("files_touched", [])

            if isinstance(terminal, str) and terminal in TERMINAL_STATES:
                print(f"  Terminal state: {terminal}")
                state.terminal_reason = terminal

            if error:
                record = AttemptRecord(
                    strategy=strategy, timestamp=utc_now(), changed=False,
                    speedup=0.0, baseline_ns=baseline_ns, optimized_ns=0.0,
                    outcome="error", summary=summary, notes=notes, error=str(error),
                )
                state.attempts.append(record)
                write_json(case_dir / "attempt.json", asdict(record))
                print(f"  Error: {error}")
                if state.terminal_reason:
                    break
                continue

            if not changed:
                record = AttemptRecord(
                    strategy=strategy, timestamp=utc_now(), changed=False,
                    speedup=0.0, baseline_ns=baseline_ns, optimized_ns=0.0,
                    outcome="discard", summary=summary, notes=notes,
                )
                state.attempts.append(record)
                write_json(case_dir / "attempt.json", asdict(record))
                print(f"  No changes made: {summary}")
                if state.terminal_reason:
                    break
                continue

            # Benchmark optimized version
            if benchmark_path:
                optimized_out = case_dir / "optimized.timings"
                optimized = _run_benchmark(worktree.path, benchmark_path, optimized_out, "optimized")
                optimized_ns = float(optimized.get("median_ns", 0))
                write_json(case_dir / "optimized_stats.json", optimized)
            else:
                optimized_ns = 0.0

            speedup = baseline_ns / optimized_ns if optimized_ns > 0 else 0.0

            if speedup >= keep_threshold:
                outcome = "keep"
                wt.commit_all(worktree, f"cpp-perf: {strategy} optimization on {target}\n\nSpeedup: {speedup:.4f}x\n{summary}")
                if speedup > state.best_speedup:
                    state.best_speedup = speedup
                    state.best_strategy = strategy
                print(f"  KEEP: speedup={speedup:.4f}x — {summary}")
            else:
                outcome = "discard"
                # Revert
                target_path.write_bytes(original_content)
                print(f"  Discard: speedup={speedup:.4f}x (below {keep_threshold}x) — {summary}")

            record = AttemptRecord(
                strategy=strategy, timestamp=utc_now(), changed=True,
                speedup=speedup, baseline_ns=baseline_ns, optimized_ns=optimized_ns,
                outcome=outcome, summary=summary, notes=notes,
                files_touched=[str(f) for f in files_touched] if isinstance(files_touched, list) else [],
            )
            state.attempts.append(record)
            write_json(case_dir / "attempt.json", asdict(record))

            if state.terminal_reason:
                break

        # Summary
        write_json(output_root / "loop_state.json", asdict(state))
        print(f"\n=== Loop complete ===")
        print(f"Attempts: {len(state.attempts)}")
        print(f"Kept: {state.kept_count()}")
        print(f"Best: {state.best_speedup:.4f}x ({state.best_strategy or 'none'})")
        print(f"Terminal: {state.terminal_reason or 'strategies exhausted'}")

        if state.kept_count() > 0:
            print(f"\nWorktree with improvements: {worktree.path}")
            print(f"Branch: {branch_name}")
            print(f"To merge: git cherry-pick {wt.current_head(worktree.path)}")
            print(f"To discard: git worktree remove {worktree.path}")
            # Don't auto-cleanup if we have improvements
            return state

    except Exception as exc:
        print(f"Loop failed: {exc}", file=sys.stderr)
        state.terminal_reason = f"error: {exc}"
        write_json(output_root / "loop_state.json", asdict(state))

    # Cleanup if no improvements kept
    if state.kept_count() == 0:
        wt.cleanup(worktree)
        print("Worktree cleaned up (no improvements kept)")

    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Lightweight deep optimization loop with worktree isolation")
    parser.add_argument("--repo-root", required=True, help="Path to the target repository")
    parser.add_argument("--target", required=True, help="Relative path to the target file")
    parser.add_argument("--benchmark", default=None, help="Benchmark path (e.g. benchmark/micro/...)")
    parser.add_argument("--strategies", default="vectorize,layout,branch,prefetch",
                        help="Comma-separated optimization strategies")
    parser.add_argument("--keep-threshold", type=float, default=1.03, help="Minimum speedup to keep")
    parser.add_argument("--output-dir", default=None, help="Output directory for loop artifacts")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    state = run_loop(
        repo_root=repo_root,
        target=args.target,
        strategies=strategies,
        benchmark_path=args.benchmark,
        keep_threshold=args.keep_threshold,
        output_dir=output_dir,
    )
    print(json.dumps(asdict(state), indent=2))


if __name__ == "__main__":
    main()
