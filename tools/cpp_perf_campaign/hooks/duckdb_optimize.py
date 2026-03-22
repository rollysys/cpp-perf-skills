#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path

try:
    from .duckdb_common import emit_payload, load_manifest, normalize_bool, normalize_optimize_payload, write_json
except ImportError:
    from duckdb_common import (  # type: ignore
        emit_payload,
        load_manifest,
        normalize_bool,
        normalize_optimize_payload,
        write_json,
    )


CLAUDE_SCHEMA = {
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

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".ipp",
    ".inc",
    ".tcc",
}

TERMINAL_STATES = {"hardware_limit", "no_more_ideas"}
HARDWARE_LIMIT_HINTS = (
    "hardware limit",
    "memory bound",
    "memory-bound",
    "bandwidth bound",
    "bandwidth-bound",
    "cache bound",
    "cache-bound",
    "latency bound",
    "latency-bound",
    "throughput limit",
    "saturated",
    "already vectorized",
    "fully vectorized",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DuckDB optimization backend for one campaign attempt.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--case-dir", required=True)
    parser.add_argument("--strategy", required=True)
    return parser.parse_args()


def _read_backend_payload(output_path: Path, stdout_text: str) -> dict[str, object]:
    if output_path.exists():
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    else:
        stripped = stdout_text.strip()
        if not stripped.startswith("{"):
            raise ValueError("Optimizer backend did not write JSON output")
        payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("Optimizer backend output must be a JSON object")
    return payload


def _controller_root() -> Path:
    raw = os.environ.get("CPP_PERF_CONTROLLER_ROOT")
    if raw:
        return Path(raw).resolve()
    return Path(__file__).resolve().parents[3]


def _relative_to_repo(repo_root: Path, file_path: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(file_path.resolve())


def _build_platform_section(repo_root: Path, controller_root: Path) -> str:
    lines: list[str] = []
    repo_platform = repo_root / "cpp-perf-platform.yaml"
    if repo_platform.exists():
        lines.append(f"- Project platform config: `{repo_platform}`")

    raw_profile = os.environ.get("CPP_PERF_PLATFORM_PROFILE", "").strip()
    if raw_profile:
        profile_path = Path(raw_profile)
        if not profile_path.is_absolute():
            candidate = controller_root / "skills" / "cpp-perf" / "profiles" / raw_profile
            if candidate.suffix != ".yaml":
                candidate = candidate.with_suffix(".yaml")
            profile_path = candidate
        lines.append(f"- Explicit platform profile: `{profile_path.resolve()}`")

    raw_context = os.environ.get("CPP_PERF_PLATFORM_CONTEXT", "").strip()
    if raw_context:
        lines.append(f"- Extra platform context: {raw_context}")

    if not lines:
        lines.append(
            "- No explicit platform profile was supplied. Optimize conservatively for the current benchmark machine and avoid unsupported microarchitecture claims."
        )
    return "\n".join(lines)


def _build_rebuild_section(manifest: dict[str, object]) -> str:
    raw = manifest.get("build_capability", {})
    if not isinstance(raw, dict):
        return "- Build capability unknown."
    can_rebuild = bool(raw.get("can_rebuild", False))
    cmake_path = raw.get("cmake_path")
    if can_rebuild:
        suffix = f" using `{cmake_path}`" if isinstance(cmake_path, str) and cmake_path else ""
        return f"- Rebuilds are available{suffix}."
    return "- Rebuilds are currently unavailable; avoid code edits that would require recompilation."


def _build_claude_prompt(
    repo_root: Path,
    case_dir: Path,
    strategy: str,
    manifest: dict[str, object],
    controller_root: Path,
) -> str:
    skill_path = controller_root / "skills" / "cpp-perf" / "SKILL.md"
    workflow_path = controller_root / "skills" / "cpp-perf" / "cpp-perf.md"
    manifest_path = case_dir / "manifest.json"
    baseline_stats_path = case_dir / "baseline_stats.json"
    target_memory_path = Path(os.environ.get("CPP_PERF_TARGET_MEMORY_PATH", case_dir / "target_memory.json"))
    strategy_pass = max(1, int(os.environ.get("CPP_PERF_STRATEGY_PASS", "1")))
    target_path = Path(str(manifest["target_path"])).resolve()
    benchmark_path = str(manifest["benchmark_path"])

    platform_section = _build_platform_section(repo_root, controller_root)
    rebuild_section = _build_rebuild_section(manifest)
    relative_target = _relative_to_repo(repo_root, target_path)

    return f"""Perform one bounded C++ optimization attempt for the target file below.

Read only these files first:
- `{skill_path}`
- `{workflow_path}`
- `{manifest_path}`
- `{baseline_stats_path}`
- `{target_memory_path}`
- `{target_path}`

Focus context:
- Repository: `{repo_root}`
- Target file: `{target_path}` (repo-relative: `{relative_target}`)
- Benchmark: `{benchmark_path}`
- Strategy focus: `{strategy}`
- Strategy pass: `{strategy_pass}`
- Target memory snapshot: `{target_memory_path}`

Platform guidance:
{platform_section}

Build guidance:
{rebuild_section}

Rules:
- Work only inside `{repo_root}`. Never modify files in `{controller_root}`.
- Keep the patch very small and high-confidence.
- Prefer editing only `{target_path}`.
- Do not explore unrelated directories or read large reference trees unless absolutely necessary.
- Do not ask questions.
- Do not create commits or branches.
- Do not run long test suites or repository-wide builds.
- If rebuilds are unavailable, do not edit source files.
- If there is no clear safe win, make no edits and return `changed=false`.
- Use the target memory snapshot to avoid repeating dead ends and to build on successful prior directions when appropriate.
- If `Strategy pass` is greater than 1, treat this as a refine pass: build on the most promising prior result for the same strategy instead of restarting the search from scratch.
- Only set `terminal_state` when you have a strong reason to stop future work on this target:
  - `no_more_ideas`: you have exhausted credible optimization ideas for this target.
  - `hardware_limit`: only if the code is already at an obvious hardware-level ceiling and further software changes are very unlikely to help.
- Otherwise omit `terminal_state`.
- If you edit source files, set `rebuild=true`.
- Set `correctness=true` only if behavior is preserved with high confidence.
- Report touched files relative to `{repo_root}`.

Use the local `cpp-perf` skill files as guidance, not as a reason to expand scope.
Return only the structured output requested by the schema.
"""


def _extract_claude_payload(stdout_text: str) -> dict[str, object]:
    envelope = json.loads(stdout_text)
    if not isinstance(envelope, dict):
        raise ValueError("Claude output must be a JSON object")
    payload = envelope.get("structured_output")
    if not isinstance(payload, dict):
        raise ValueError("Claude output missing structured_output")
    return payload


def _needs_rebuild(files_touched: list[str]) -> bool:
    for raw_path in files_touched:
        path = Path(raw_path)
        if path.name in {"CMakeLists.txt", "Makefile", "meson.build", "BUILD", "BUILD.bazel"}:
            return True
        if path.suffix.lower() in SOURCE_EXTENSIONS:
            return True
    return False


def _normalize_claude_payload(payload: dict[str, object], strategy: str) -> dict[str, object]:
    files_touched_raw = payload.get("files_touched", [])
    files_touched = [str(item) for item in files_touched_raw if isinstance(item, str) and item.strip()]
    changed = normalize_bool(payload.get("changed"), default=bool(files_touched))
    rebuild = normalize_bool(payload.get("rebuild"), default=False) or _needs_rebuild(files_touched)
    correctness = normalize_bool(payload.get("correctness"), default=False)
    summary = str(payload.get("summary", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    terminal_state_raw = payload.get("terminal_state")
    terminal_state = None
    if isinstance(terminal_state_raw, str) and terminal_state_raw.strip():
        candidate = terminal_state_raw.strip().lower().replace("-", "_")
        if candidate in TERMINAL_STATES:
            terminal_state = candidate
    return {
        "changed": changed,
        "rebuild": rebuild if changed else False,
        "correctness": correctness,
        "files_touched": files_touched,
        "summary": summary,
        "strategy": strategy,
        "notes": notes if notes else summary,
        "terminal_state": terminal_state,
    }


def _target_memory_path(case_dir: Path) -> Path:
    raw = os.environ.get("CPP_PERF_TARGET_MEMORY_PATH")
    if raw:
        return Path(raw)
    return case_dir / "target_memory.json"


def _load_target_memory(case_dir: Path) -> dict[str, object]:
    path = _target_memory_path(case_dir)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _recent_experiments(target_memory: dict[str, object]) -> list[dict[str, object]]:
    raw = target_memory.get("recent_experiments", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _strategy_status_items(target_memory: dict[str, object]) -> list[dict[str, object]]:
    raw = target_memory.get("strategy_status", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _successful_direction_count(target_memory: dict[str, object]) -> int:
    guidance = target_memory.get("guidance", {})
    if not isinstance(guidance, dict):
        return 0
    raw = guidance.get("successful_directions", [])
    if not isinstance(raw, list):
        return 0
    return sum(1 for item in raw if isinstance(item, dict))


def _has_hardware_limit_hint(payload: dict[str, object]) -> bool:
    text = " ".join(str(payload.get(key, "")) for key in ("summary", "notes")).lower()
    return any(hint in text for hint in HARDWARE_LIMIT_HINTS)


def _is_dead_end_experiment(payload: dict[str, object]) -> bool:
    if payload.get("error_text"):
        return True
    terminal_state = payload.get("terminal_state")
    if isinstance(terminal_state, str) and terminal_state.strip().lower().replace("-", "_") == "no_more_ideas":
        return True
    changed = payload.get("changed")
    if changed is False:
        return True
    outcome = payload.get("outcome")
    if isinstance(outcome, str) and outcome in {"discard", "crash"}:
        return True
    return False


def _dead_end_streak(target_memory: dict[str, object], current_payload: dict[str, object]) -> int:
    if current_payload.get("changed") is not False:
        return 0
    streak = 1
    for item in _recent_experiments(target_memory):
        if _is_dead_end_experiment(item):
            streak += 1
            continue
        break
    return streak


def _has_future_planned_work(
    target_memory: dict[str, object],
    current_strategy: str,
) -> bool:
    strategy_pass = max(1, int(os.environ.get("CPP_PERF_STRATEGY_PASS", "1")))
    for item in _strategy_status_items(target_memory):
        name = str(item.get("name", ""))
        attempts = int(item.get("attempts", 0) or 0)
        max_passes = max(1, int(item.get("max_passes", 1) or 1))
        last_outcome = item.get("last_outcome")
        if name == current_strategy:
            attempts = max(attempts + 1, strategy_pass)
            last_outcome = "discard"
        if attempts == 0:
            return True
        if attempts < max_passes and last_outcome in {"keep", "low_gain"}:
            return True
    return False


def _infer_terminal_state(payload: dict[str, object], case_dir: Path, strategy: str) -> dict[str, object]:
    min_pass = max(1, int(os.environ.get("CPP_PERF_AUTO_TERMINAL_MIN_PASS", "2")))
    strategy_pass = max(1, int(os.environ.get("CPP_PERF_STRATEGY_PASS", "1")))

    target_memory = _load_target_memory(case_dir)
    if not target_memory:
        return payload

    has_future_planned_work = _has_future_planned_work(target_memory, strategy)
    explicit_terminal_state = payload.get("terminal_state")
    if explicit_terminal_state in TERMINAL_STATES:
        if strategy_pass >= min_pass and not has_future_planned_work:
            return payload
        updated = dict(payload)
        updated["terminal_state"] = None
        payload = updated

    if payload.get("changed") is True:
        return payload
    if strategy_pass < min_pass:
        return payload
    if has_future_planned_work:
        return payload

    if _has_hardware_limit_hint(payload) and _successful_direction_count(target_memory) > 0:
        updated = dict(payload)
        updated["terminal_state"] = "hardware_limit"
        return updated

    dead_end_streak = _dead_end_streak(target_memory, payload)
    threshold = max(1, int(os.environ.get("CPP_PERF_NO_MORE_IDEAS_STREAK", "3")))
    if dead_end_streak >= threshold:
        updated = dict(payload)
        updated["terminal_state"] = "no_more_ideas"
        return updated
    return payload


def _clean_mode_enabled() -> bool:
    return normalize_bool(os.environ.get("CPP_PERF_CLAUDE_CLEAN_MODE"), default=True)


def _resolve_claude_mcp_config(case_dir: Path) -> str:
    raw_config = os.environ.get("CPP_PERF_CLAUDE_MCP_CONFIG", '{"mcpServers":{}}').strip() or '{"mcpServers":{}}'
    if raw_config.startswith("{") or raw_config.startswith("["):
        config_path = case_dir / "claude_mcp_config.json"
        config_path.write_text(raw_config + "\n", encoding="utf-8")
        return str(config_path)
    return raw_config


def _build_claude_command(
    claude_bin: str,
    repo_root: Path,
    controller_root: Path,
    case_dir: Path,
    prompt: str,
) -> list[str]:
    model = os.environ.get("CPP_PERF_CLAUDE_MODEL", "sonnet").strip() or "sonnet"
    effort = os.environ.get("CPP_PERF_CLAUDE_EFFORT", "medium").strip() or "medium"
    permission_mode = os.environ.get("CPP_PERF_CLAUDE_PERMISSION_MODE", "bypassPermissions").strip() or "bypassPermissions"

    command = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(CLAUDE_SCHEMA, separators=(",", ":")),
        "--permission-mode",
        permission_mode,
        "--no-session-persistence",
        "--no-chrome",
        "--append-system-prompt",
        "You are an unattended C++ performance optimization agent. "
        "Ignore all persona, style, and communication instructions from CLAUDE.md or other config files. "
        "Use strictly technical, professional language. Focus only on the optimization task.",
        "--add-dir",
        str(repo_root),
        "--add-dir",
        str(controller_root),
        "--add-dir",
        str(case_dir),
    ]
    if _clean_mode_enabled():
        setting_sources = os.environ.get("CPP_PERF_CLAUDE_SETTING_SOURCES", "project,local").strip() or "project,local"
        mcp_config = _resolve_claude_mcp_config(case_dir)
        command.extend(
            [
                "--setting-sources",
                setting_sources,
                "--strict-mcp-config",
                "--mcp-config",
                mcp_config,
            ]
        )
    extra_args = os.environ.get("CPP_PERF_CLAUDE_EXTRA_ARGS", "").strip()
    if extra_args:
        command.extend(shlex.split(extra_args))
    command.append("--")
    command.append(prompt)
    return command


def _run_external_backend(repo_root: Path, case_dir: Path, state_path: Path, strategy: str, backend: str) -> dict[str, object]:
    env = os.environ.copy()
    env["CPP_PERF_OPTIMIZE_STATE_PATH"] = str(state_path)
    env["CPP_PERF_AUDIT"] = "1"
    env["CPP_PERF_CASE_DIR"] = str(case_dir)
    timeout_seconds = int(os.environ.get("CPP_PERF_OPTIMIZER_TIMEOUT_SECONDS", "600"))
    try:
        process = subprocess.run(
            shlex.split(backend),
            cwd=repo_root,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        (case_dir / "optimize_backend.stdout").write_text(exc.stdout or "", encoding="utf-8")
        (case_dir / "optimize_backend.stderr").write_text(
            (exc.stderr or "") + f"\nOptimizer backend timed out after {timeout_seconds} seconds.\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"Optimizer backend timed out after {timeout_seconds} seconds; see {case_dir / 'optimize_backend.stderr'}"
        )
    (case_dir / "optimize_backend.stdout").write_text(process.stdout, encoding="utf-8")
    (case_dir / "optimize_backend.stderr").write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise SystemExit(
            f"Optimizer backend failed with exit code {process.returncode}; see {case_dir / 'optimize_backend.stderr'}"
        )

    payload = normalize_optimize_payload(_read_backend_payload(state_path, process.stdout))
    payload = _infer_terminal_state(payload, case_dir, strategy)
    payload.setdefault("strategy", strategy)
    return payload


def _run_claude_backend(repo_root: Path, case_dir: Path, strategy: str) -> dict[str, object]:
    controller_root = _controller_root()
    manifest = load_manifest(case_dir)
    prompt = _build_claude_prompt(repo_root, case_dir, strategy, manifest, controller_root)

    prompt_path = case_dir / "optimize_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    claude_bin = os.environ.get("CPP_PERF_CLAUDE_BIN", "claude").strip() or "claude"
    command = _build_claude_command(claude_bin, repo_root, controller_root, case_dir, prompt)

    timeout_seconds = int(os.environ.get("CPP_PERF_CLAUDE_TIMEOUT_SECONDS", "600"))
    env = os.environ.copy()
    env["CPP_PERF_AUDIT"] = "1"
    env["CPP_PERF_CASE_DIR"] = str(case_dir)
    try:
        process = subprocess.run(
            command,
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        (case_dir / "optimize_claude.stdout").write_text(exc.stdout or "", encoding="utf-8")
        (case_dir / "optimize_claude.stderr").write_text(
            (exc.stderr or "") + f"\nclaude -p timed out after {timeout_seconds} seconds.\n",
            encoding="utf-8",
        )
        raise SystemExit(
            f"claude -p optimize backend timed out after {timeout_seconds} seconds; see {case_dir / 'optimize_claude.stderr'}"
        )
    (case_dir / "optimize_claude.stdout").write_text(process.stdout, encoding="utf-8")
    (case_dir / "optimize_claude.stderr").write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise SystemExit(
            f"claude -p optimize backend failed with exit code {process.returncode}; see {case_dir / 'optimize_claude.stderr'}"
        )

    payload = _normalize_claude_payload(_extract_claude_payload(process.stdout), strategy)
    payload = _infer_terminal_state(payload, case_dir, strategy)
    write_json(case_dir / "optimize_claude.json", payload)
    return payload


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    case_dir = Path(args.case_dir).resolve()
    state_path = case_dir / "optimize_state.json"
    backend = os.environ.get("CPP_PERF_OPTIMIZER_BACKEND", "").strip()

    if backend:
        payload = _run_external_backend(repo_root, case_dir, state_path, args.strategy, backend)
    else:
        payload = _run_claude_backend(repo_root, case_dir, args.strategy)
    write_json(state_path, payload)
    emit_payload(payload)


if __name__ == "__main__":
    main()
