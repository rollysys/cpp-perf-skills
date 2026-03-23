"""Lightweight deep optimization loop with worktree isolation.

Usage:
    # Single function
    python3 -m tools.cpp_perf_campaign.optimize_loop \\
        --repo-root /path/to/repo \\
        --target src/execution/join_hashtable.cpp \\
        --function InsertHashesLoop

    # Auto top-N functions from a file
    python3 -m tools.cpp_perf_campaign.optimize_loop \\
        --repo-root /path/to/repo \\
        --target src/execution/join_hashtable.cpp \\
        --top-n 3

    # Batch: multiple files
    python3 -m tools.cpp_perf_campaign.optimize_loop \\
        --repo-root /path/to/repo \\
        --targets src/execution/join_hashtable.cpp src/storage/table/update_segment.cpp \\
        --top-n 2
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


# ── Data structures ────────────────────────────────────────────────────

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
    function_name: str | None = None
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
        "measured_baseline_ns": {"type": "number"},
        "measured_optimized_ns": {"type": "number"},
    },
    "required": ["changed", "rebuild", "correctness", "files_touched", "summary", "notes"],
    "additionalProperties": False,
}

TERMINAL_STATES = {"hardware_limit", "no_more_ideas"}

CONTROLLER_ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_SETTINGS = CONTROLLER_ROOT / ".claude" / "settings.json"

# Timeout scaling by scope depth
TIMEOUT_BY_DEPTH = {
    "shallow": 300,
    "medium": 600,
    "deep": 900,
}


# ── Prompt construction ────────────────────────────────────────────────

def _build_optimize_prompt(
    target_path: Path,
    strategy: str,
    state: LoopState,
    case_dir: Path,
    scope_context: str = "",
    function_name: str | None = None,
    function_lines: str | None = None,
) -> str:
    history_section = ""
    if state.attempts:
        lines = []
        for a in state.attempts:
            lines.append(f"- {a.strategy}: {a.outcome}, speedup={a.speedup:.4f}, summary={a.summary}")
        history_section = f"\nPrevious attempts on this target:\n" + "\n".join(lines) + "\n"

    scope_section = f"\n{scope_context}\n" if scope_context else ""

    if function_name:
        focus = f"Function: `{function_name}` (lines {function_lines})"
    else:
        focus = "Optimize the most impactful function in the file."

    return f"""Perform one bounded C++ optimization attempt.

Target file: `{target_path}`
{focus}
Strategy focus: `{strategy}`
{scope_section}{history_section}
Rules:
- Read the target file first.
- Focus ONLY on the specified function. Do not optimize other functions.
- Keep the patch small and high-confidence.

Benchmarking (MANDATORY):
- You MUST write a STANDALONE benchmark.cpp that compiles with `c++ -O2 -o benchmark benchmark.cpp`.
- The benchmark must be SELF-CONTAINED: copy the function, synthesize inputs, measure timing.
- Do NOT build the whole project. Do NOT use cmake/make on the project.
- Do NOT use the project's test or benchmark infrastructure.
- If you need type definitions for the function's parameters or return type,
  use the find_definition LSP tool (if available) to locate and copy them.
  Alternatively, read the relevant header files directly.
- Keep copied types minimal — only what's needed to compile the benchmark.
- Run the benchmark BEFORE and AFTER optimization to get measured speedup.
- Do NOT claim speedup without measured evidence.
- Report measured_baseline_ns and measured_optimized_ns in the structured output.

Optimization:
- Prefer editing only the target file.
- Do not explore unrelated directories.
- Do not ask questions or create commits.
- If there is no clear safe win for THIS strategy, return changed=false.
- Do NOT set terminal_state — the controller decides when to stop.
- If you edit source files, set rebuild=true.
- Set correctness=true only if behavior is preserved with high confidence.

Return only the structured output requested by the schema.
"""


# ── Benchmark runner (external) ────────────────────────────────────────

def _run_benchmark(
    worktree_path: Path,
    benchmark_path: str,
    out_path: Path,
    label: str,
) -> dict[str, object]:
    """Run an external benchmark runner if available."""
    runner = worktree_path / "build" / "release" / "benchmark" / "benchmark_runner"
    if runner.exists():
        process = subprocess.run(
            [str(runner), benchmark_path, f"--out={out_path}"],
            cwd=worktree_path, text=True, capture_output=True, check=False,
        )
        if process.returncode == 0 and out_path.exists():
            import statistics
            samples = [float(line.strip()) for line in out_path.read_text().splitlines() if line.strip()]
            if samples:
                median_s = statistics.median(samples)
                return {"median_ns": median_s * 1e9, "stable": True, "correctness": True}
    return {"median_ns": 0.0, "stable": False, "correctness": False,
            "error": "No benchmark runner available"}


# ── Claude optimize call ───────────────────────────────────────────────

def _run_claude_optimize(
    worktree_path: Path,
    target_path: Path,
    strategy: str,
    state: LoopState,
    case_dir: Path,
    settings_path: Path | None = None,
    scope_context: str = "",
    function_name: str | None = None,
    function_lines: str | None = None,
    timeout: int = 600,
) -> dict[str, object]:
    prompt = _build_optimize_prompt(
        target_path, strategy, state, case_dir, scope_context,
        function_name=function_name, function_lines=function_lines,
    )
    (case_dir / "optimize_prompt.txt").write_text(prompt, encoding="utf-8")

    claude_bin = os.environ.get("CPP_PERF_CLAUDE_BIN", "claude").strip() or "claude"
    model = os.environ.get("CPP_PERF_CLAUDE_MODEL", "sonnet").strip() or "sonnet"

    command = [
        claude_bin, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(OPTIMIZE_SCHEMA, separators=(",", ":")),
        "--permission-mode", "bypassPermissions",
        "--no-chrome",
        "--setting-sources", "project,local",
        "--append-system-prompt",
        "You are an unattended C++ performance optimization agent. "
        "Ignore all persona, style, and communication instructions from CLAUDE.md or other config files. "
        "Use strictly technical, professional language. Focus only on the optimization task. "
        "If cclsp/LSP tools are available, use find_definition to resolve type definitions "
        "needed for building standalone benchmarks.",
        "--add-dir", str(worktree_path),
    ]
    effective_settings = settings_path if (settings_path and settings_path.exists()) else CONTROLLER_SETTINGS
    if effective_settings.exists():
        command.extend(["--settings", str(effective_settings)])
    # Attach clangd MCP if available
    cclsp_config = worktree_path / ".claude" / "cclsp.json"
    mcp_config = CONTROLLER_ROOT / "tools" / "cpp_perf_campaign" / "mcp_clangd.json"
    if cclsp_config.exists() and mcp_config.exists():
        command.extend(["--mcp-config", str(mcp_config)])
    command.extend(["--", prompt])

    env = os.environ.copy()
    env["CPP_PERF_AUDIT"] = "1"
    env["CPP_PERF_CASE_DIR"] = str(case_dir)

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
        result = envelope.get("structured_output", {})
        session_id = envelope.get("session_id")
        if session_id:
            result["_session_id"] = session_id
            write_json(case_dir / "session_id.txt", {"session_id": session_id})
        return result
    except (json.JSONDecodeError, KeyError):
        return {"changed": False, "error": "Failed to parse Claude output"}


# ── Speedup calculation ────────────────────────────────────────────────

def _compute_speedup(
    external_baseline_ns: float,
    external_optimized_ns: float,
    optimize_result: dict[str, object],
) -> tuple[float, float, float]:
    """Compute speedup from external benchmark or agent's own measurements.

    Returns (speedup, baseline_ns, optimized_ns).
    Prefers external benchmark data; falls back to agent-reported measurements.
    """
    # External benchmark data (from project's benchmark runner)
    if external_baseline_ns > 0 and external_optimized_ns > 0:
        return external_baseline_ns / external_optimized_ns, external_baseline_ns, external_optimized_ns

    # Agent-reported measurements (from standalone benchmark)
    agent_baseline = float(optimize_result.get("measured_baseline_ns") or 0)
    agent_optimized = float(optimize_result.get("measured_optimized_ns") or 0)
    if agent_baseline > 0 and agent_optimized > 0:
        return agent_baseline / agent_optimized, agent_baseline, agent_optimized

    return 0.0, 0.0, 0.0


# ── Core loop ──────────────────────────────────────────────────────────

def run_loop(
    repo_root: Path,
    target: str,
    strategies: list[str] | None = None,
    benchmark_path: str | None = None,
    keep_threshold: float = 1.03,
    output_dir: Path | None = None,
    function_name: str | None = None,
) -> LoopState:
    """Run the full optimization loop with worktree isolation."""
    from .scope_analyzer import analyze, analyze_file, extract_functions, profile_to_prompt_context

    output_root = output_dir or (repo_root / ".cpp-perf" / "loops" / target.replace("/", "_"))
    ensure_dir(output_root)
    settings_path = repo_root / ".claude" / "settings.json"

    target_source = repo_root / target
    if not target_source.exists():
        print(f"Target not found: {target_source}")
        return LoopState(target=target, repo_root=str(repo_root),
                         strategies=[], terminal_reason="target_not_found")

    # Function-level analysis
    function_lines = None
    if function_name:
        functions = extract_functions(target_source)
        match = [f for f in functions if f.name == function_name]
        if not match:
            match = [f for f in functions if function_name in f.name]
        if match:
            func_target = match[0]
            scope_profile = func_target.profile
            function_lines = func_target.line_range
            print(f"Function: {func_target.name} L{function_lines}")
        else:
            print(f"Function '{function_name}' not found. Available:")
            for f in functions[:10]:
                print(f"  {f.name} L{f.line_range}")
            return LoopState(target=target, repo_root=str(repo_root),
                             strategies=[], terminal_reason="function_not_found")
    else:
        scope_profile = analyze_file(target_source)

    scope_context = profile_to_prompt_context(scope_profile)
    write_json(output_root / "scope_profile.json", asdict(scope_profile))
    print(f"Scope: {scope_profile.code_type} / {scope_profile.scope_depth}")

    if scope_profile.skip_reason:
        print(f"Skipping: {scope_profile.skip_reason}")
        return LoopState(target=target, repo_root=str(repo_root),
                         strategies=[], terminal_reason=f"skip:{scope_profile.skip_reason}")

    # Extract compilation context if compile_commands.json exists
    compile_context = ""
    cc_path = repo_root / "compile_commands.json"
    if not cc_path.exists():
        cc_path = repo_root / "build" / "dev" / "compile_commands.json"
    if cc_path.exists() and function_name:
        try:
            from .extract_context import extract_context as do_extract
            ctx = do_extract(cc_path, target, function_name, repo_root, output_root)
            if "error" not in ctx:
                compile_context = (
                    f"\n## Compilation Context (auto-extracted)\n"
                    f"- Compile with: `c++ {ctx['compile_flags_inline']} benchmark.cpp`\n"
                    f"- Key includes: {', '.join(ctx['source_includes'][:5])}\n"
                    f"- Type→header mapping: {json.dumps(ctx['type_headers'], indent=2)}\n"
                    f"- {ctx['hint']}\n"
                )
                print(f"Extracted compile context: {len(ctx['type_headers'])} type mappings")
        except Exception as exc:
            print(f"Context extraction failed (non-fatal): {exc}")

    scope_context = scope_context + compile_context

    # Timeout scaling by scope depth
    base_timeout = int(os.environ.get("CPP_PERF_CLAUDE_TIMEOUT_SECONDS", "0"))
    if base_timeout <= 0:
        base_timeout = TIMEOUT_BY_DEPTH.get(scope_profile.scope_depth, 600)
    print(f"Timeout per strategy: {base_timeout}s ({scope_profile.scope_depth})")

    # Strategy selection
    if not strategies:
        strategies = scope_profile.recommended_strategies
        print(f"Auto strategies ({scope_profile.max_strategies}): {', '.join(strategies)}")
    else:
        print(f"User strategies: {', '.join(strategies)}")

    state = LoopState(
        target=target,
        repo_root=str(repo_root),
        strategies=strategies,
        function_name=function_name,
    )

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

            original_content = target_path.read_bytes()
            print(f"\n--- Strategy: {strategy} ---")

            # External baseline (if benchmark runner available)
            ext_baseline_ns = 0.0
            if benchmark_path:
                baseline_out = case_dir / "baseline.timings"
                baseline = _run_benchmark(worktree.path, benchmark_path, baseline_out, "baseline")
                ext_baseline_ns = float(baseline.get("median_ns", 0))
                write_json(case_dir / "baseline_stats.json", baseline)

            # Optimize
            optimize_result = _run_claude_optimize(
                worktree.path, target_path, strategy, state, case_dir, settings_path,
                scope_context=scope_context,
                function_name=function_name,
                function_lines=function_lines,
                timeout=base_timeout,
            )
            write_json(case_dir / "optimize_result.json", optimize_result)

            changed = bool(optimize_result.get("changed", False))
            summary = str(optimize_result.get("summary", ""))
            notes = str(optimize_result.get("notes", ""))
            error = optimize_result.get("error")
            files_touched = optimize_result.get("files_touched", [])

            if error:
                record = AttemptRecord(
                    strategy=strategy, timestamp=utc_now(), changed=False,
                    speedup=0.0, baseline_ns=ext_baseline_ns, optimized_ns=0.0,
                    outcome="error", summary=summary, notes=notes, error=str(error),
                )
                state.attempts.append(record)
                write_json(case_dir / "attempt.json", asdict(record))
                print(f"  Error: {error}")
                continue

            if not changed:
                record = AttemptRecord(
                    strategy=strategy, timestamp=utc_now(), changed=False,
                    speedup=0.0, baseline_ns=ext_baseline_ns, optimized_ns=0.0,
                    outcome="discard", summary=summary, notes=notes,
                )
                state.attempts.append(record)
                write_json(case_dir / "attempt.json", asdict(record))
                print(f"  No changes made: {summary}")
                continue

            # Compute speedup from external benchmark or agent measurements
            ext_optimized_ns = 0.0
            if benchmark_path:
                optimized_out = case_dir / "optimized.timings"
                optimized = _run_benchmark(worktree.path, benchmark_path, optimized_out, "optimized")
                ext_optimized_ns = float(optimized.get("median_ns", 0))
                write_json(case_dir / "optimized_stats.json", optimized)

            speedup, baseline_ns, optimized_ns = _compute_speedup(
                ext_baseline_ns, ext_optimized_ns, optimize_result,
            )

            if speedup >= keep_threshold:
                outcome = "keep"
                wt.commit_all(worktree,
                    f"cpp-perf: {strategy} optimization on {target}"
                    + (f"::{function_name}" if function_name else "")
                    + f"\n\nSpeedup: {speedup:.4f}x\n{summary}"
                )
                if speedup > state.best_speedup:
                    state.best_speedup = speedup
                    state.best_strategy = strategy
                print(f"  KEEP: speedup={speedup:.4f}x — {summary}")
            else:
                outcome = "discard"
                target_path.write_bytes(original_content)
                if speedup > 0:
                    print(f"  Discard: speedup={speedup:.4f}x (below {keep_threshold}x) — {summary}")
                else:
                    print(f"  Discard: no measured speedup — {summary}")

            record = AttemptRecord(
                strategy=strategy, timestamp=utc_now(), changed=True,
                speedup=speedup, baseline_ns=baseline_ns, optimized_ns=optimized_ns,
                outcome=outcome, summary=summary, notes=notes,
                files_touched=[str(f) for f in files_touched] if isinstance(files_touched, list) else [],
            )
            state.attempts.append(record)
            write_json(case_dir / "attempt.json", asdict(record))

        # Summary
        if not state.terminal_reason:
            state.terminal_reason = "all_strategies_exhausted"
        write_json(output_root / "loop_state.json", asdict(state))
        _print_summary(state)

        if state.kept_count() > 0:
            print(f"\nWorktree with improvements: {worktree.path}")
            print(f"Branch: {branch_name}")
            print(f"To merge: git cherry-pick {wt.current_head(worktree.path)}")
            print(f"To discard: git worktree remove {worktree.path}")
            return state

    except Exception as exc:
        print(f"Loop failed: {exc}", file=sys.stderr)
        state.terminal_reason = f"error: {exc}"
        write_json(output_root / "loop_state.json", asdict(state))

    if state.kept_count() == 0:
        wt.cleanup(worktree)
        print("Worktree cleaned up (no improvements kept)")

    return state


def _print_summary(state: LoopState) -> None:
    print(f"\n=== Loop complete ===")
    print(f"Target: {state.target}" + (f"::{state.function_name}" if state.function_name else ""))
    print(f"Attempts: {len(state.attempts)}")
    print(f"Kept: {state.kept_count()}")
    print(f"Best: {state.best_speedup:.4f}x ({state.best_strategy or 'none'})")
    print(f"Terminal: {state.terminal_reason}")


# ── Multi-function batch ───────────────────────────────────────────────

def run_batch(
    repo_root: Path,
    targets: list[str],
    top_n: int = 3,
    benchmark_path: str | None = None,
    keep_threshold: float = 1.03,
    output_dir: Path | None = None,
) -> list[LoopState]:
    """Run optimize loops on top-N functions from each target file."""
    from .scope_analyzer import extract_functions

    all_states: list[LoopState] = []
    batch_root = output_dir or (repo_root / ".cpp-perf" / "batch" / f"batch_{int(time.time())}")
    ensure_dir(batch_root)

    for target in targets:
        target_source = repo_root / target
        if not target_source.exists():
            print(f"Skipping {target}: file not found")
            continue

        functions = extract_functions(target_source)
        if not functions:
            print(f"Skipping {target}: no optimizable functions found")
            continue

        top_funcs = functions[:top_n]
        print(f"\n{'='*60}")
        print(f"File: {target} — top {len(top_funcs)} functions")
        print(f"{'='*60}")

        for func in top_funcs:
            func_output = batch_root / target.replace("/", "_") / func.name
            print(f"\n  >> {func.name} L{func.line_range} [{func.profile.scope_depth}]")
            state = run_loop(
                repo_root=repo_root,
                target=target,
                function_name=func.name,
                benchmark_path=benchmark_path,
                keep_threshold=keep_threshold,
                output_dir=func_output,
            )
            all_states.append(state)

    # Generate batch report
    report_path = generate_report(all_states, batch_root)
    print(f"\nBatch report: {report_path}")
    return all_states


# ── Report generation ──────────────────────────────────────────────────

def generate_report(states: list[LoopState], output_dir: Path) -> Path:
    """Generate a markdown summary report from loop results."""
    report_path = output_dir / "REPORT.md"
    lines = [
        "# cpp-perf Optimization Report",
        f"Generated: {utc_now()}",
        "",
    ]

    # Summary table
    total_attempts = sum(len(s.attempts) for s in states)
    total_kept = sum(s.kept_count() for s in states)
    lines.append(f"**Targets:** {len(states)} | **Attempts:** {total_attempts} | **Kept:** {total_kept}")
    lines.append("")

    # Results table
    lines.append("| Target | Function | Strategies | Best Speedup | Kept | Terminal |")
    lines.append("|---|---|---|---|---|---|")
    for s in states:
        func = s.function_name or "(file)"
        strats = len(s.attempts)
        best = f"{s.best_speedup:.2f}x" if s.best_speedup > 1.0 else "—"
        kept = s.kept_count()
        terminal = s.terminal_reason or "—"
        short_target = s.target.split("/")[-1]
        lines.append(f"| {short_target} | {func} | {strats} | {best} | {kept} | {terminal} |")

    lines.append("")

    # Detail per target
    for s in states:
        func_label = f"::{s.function_name}" if s.function_name else ""
        lines.append(f"## {s.target}{func_label}")
        lines.append("")
        if not s.attempts:
            lines.append("No attempts made.")
            lines.append("")
            continue

        for a in s.attempts:
            icon = "+" if a.outcome == "keep" else ("-" if a.outcome == "discard" else "!")
            speedup_str = f"{a.speedup:.2f}x" if a.speedup > 0 else "—"
            lines.append(f"- [{icon}] **{a.strategy}**: {a.outcome} ({speedup_str})")
            if a.summary:
                lines.append(f"  {a.summary[:200]}")
            if a.error:
                lines.append(f"  Error: {a.error}")
        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


# ── CLI ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Lightweight deep optimization loop with worktree isolation")
    parser.add_argument("--repo-root", required=True, help="Path to the target repository")

    # Single target mode
    parser.add_argument("--target", default=None, help="Single target file (relative path)")
    parser.add_argument("--function", default=None, help="Function name to optimize")
    parser.add_argument("--strategies", default=None, help="Comma-separated strategies (omit for auto)")

    # Batch mode
    parser.add_argument("--targets", nargs="+", default=None, help="Multiple target files for batch mode")
    parser.add_argument("--top-n", type=int, default=3, help="Top N functions per file in batch mode")

    # Common options
    parser.add_argument("--benchmark", default=None, help="Benchmark path")
    parser.add_argument("--keep-threshold", type=float, default=1.03, help="Minimum speedup to keep")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None

    # Batch mode
    if args.targets:
        states = run_batch(
            repo_root=repo_root,
            targets=args.targets,
            top_n=args.top_n,
            benchmark_path=args.benchmark,
            keep_threshold=args.keep_threshold,
            output_dir=output_dir,
        )
        # Print final summary
        for s in states:
            print(json.dumps({"target": s.target, "function": s.function_name,
                              "kept": s.kept_count(), "best": s.best_speedup}, indent=2))
        return

    # Single target mode
    if not args.target:
        parser.error("Either --target or --targets is required")

    strategies = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else None
    )

    state = run_loop(
        repo_root=repo_root,
        target=args.target,
        strategies=strategies,
        benchmark_path=args.benchmark,
        keep_threshold=args.keep_threshold,
        output_dir=output_dir,
        function_name=args.function,
    )
    print(json.dumps(asdict(state), indent=2))


if __name__ == "__main__":
    main()
