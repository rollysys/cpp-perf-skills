from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import statistics
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


DEFAULT_BENCHMARK = "benchmark/micro/nulls/no_nulls_addition.benchmark"
RUNNER_RELATIVE_PATH = Path("build/release/benchmark/benchmark_runner")
BUILD_COMMAND = ("make", "BUILD_BENCHMARK=1", "BUILD_TPCH=1")

BENCHMARK_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("src/execution/", "src/parallel/"), "benchmark/micro/aggregate/simple_group.benchmark", "execution"),
    (("src/function/",), "benchmark/micro/arithmetic/multiplications.benchmark", "function"),
    (("src/optimizer/",), "benchmark/micro/case/integer_case_predictable.benchmark", "optimizer"),
    (("src/storage/",), "benchmark/micro/compression/store_tpch_sf1.benchmark", "storage"),
    (("extension/parquet/",), "benchmark/parquet/dictionary_read-short-1000000.benchmark", "parquet"),
    (("extension/json/",), "benchmark/micro/cast/cast_lineitem_json_to_variant.benchmark", "json"),
    (("extension/",), "benchmark/micro/cast/cast_string_struct.benchmark", "generic_extension"),
)


@dataclass(frozen=True)
class BenchmarkSelection:
    benchmark_path: str
    reason: str


def resolve_cmake_path() -> Path | None:
    raw = os.environ.get("CPP_PERF_DUCKDB_CMAKE_BIN", "").strip()
    if raw:
        candidate = Path(raw).expanduser().resolve()
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
        return None
    discovered = shutil.which("cmake")
    if discovered:
        return Path(discovered).resolve()
    fallback = Path("/tmp/duckdb-cmake-venv/bin/cmake")
    if fallback.exists() and os.access(fallback, os.X_OK):
        return fallback.resolve()
    return None


def duckdb_build_environment() -> dict[str, object]:
    cmake_path = resolve_cmake_path()
    env = os.environ.copy()
    if cmake_path is not None:
        bin_dir = str(cmake_path.parent)
        current_path = env.get("PATH", "")
        env["PATH"] = f"{bin_dir}:{current_path}" if current_path else bin_dir
    return {
        "env": env,
        "cmake_path": str(cmake_path) if cmake_path is not None else None,
        "can_rebuild": cmake_path is not None,
    }


def build_capability_payload(repo_root: Path) -> dict[str, object]:
    build_environment = duckdb_build_environment()
    return {
        "runner_exists": benchmark_runner_path(repo_root).exists(),
        "can_rebuild": bool(build_environment["can_rebuild"]),
        "cmake_path": build_environment["cmake_path"],
    }


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def emit_payload(payload: dict[str, object]) -> None:
    result_path = os.environ.get("CPP_PERF_RESULT_PATH")
    if result_path:
        write_json(Path(result_path), payload)
    print(json.dumps(payload))


def normalize_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ok", "pass", "passed"}
    return default


def load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def relative_to_repo(repo_root: Path, target_path: Path) -> str:
    return str(target_path.resolve().relative_to(repo_root.resolve()))


def source_metrics(target_path: Path) -> dict[str, object]:
    content = target_path.read_bytes()
    text = content.decode("utf-8")
    return {
        "line_count": len(text.splitlines()),
        "byte_count": len(content),
        "sha256": sha256(content).hexdigest(),
    }


def suggest_benchmark_for_target(relative_target_path: str) -> BenchmarkSelection:
    normalized = relative_target_path.replace("\\", "/")
    for prefixes, benchmark_path, reason in BENCHMARK_RULES:
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return BenchmarkSelection(benchmark_path=benchmark_path, reason=reason)
    return BenchmarkSelection(benchmark_path=DEFAULT_BENCHMARK, reason="default")


def select_benchmark_for_target(repo_root: Path, relative_target_path: str) -> BenchmarkSelection:
    selection = suggest_benchmark_for_target(relative_target_path)
    benchmark_path = repo_root / selection.benchmark_path
    if benchmark_path.exists():
        return selection

    fallback_path = repo_root / DEFAULT_BENCHMARK
    if not fallback_path.exists():
        raise FileNotFoundError(
            f"Missing both selected benchmark '{selection.benchmark_path}' and fallback '{DEFAULT_BENCHMARK}'"
        )
    return BenchmarkSelection(
        benchmark_path=DEFAULT_BENCHMARK,
        reason=f"{selection.reason}_fallback",
    )


def benchmark_runner_path(repo_root: Path) -> Path:
    return repo_root / RUNNER_RELATIVE_PATH


def release_build_root(repo_root: Path) -> Path:
    return repo_root / "build" / "release"


def has_valid_release_build_config(repo_root: Path) -> bool:
    cache_path = release_build_root(repo_root) / "CMakeCache.txt"
    if not cache_path.exists():
        return False
    cache_text = cache_path.read_text(encoding="utf-8", errors="ignore")
    expected_root = str(repo_root.resolve())
    return expected_root in cache_text


def sanitize_release_build_dir(repo_root: Path) -> bool:
    build_root = release_build_root(repo_root)
    cache_path = build_root / "CMakeCache.txt"
    if not cache_path.exists():
        return False
    cache_text = cache_path.read_text(encoding="utf-8", errors="ignore")
    expected_root = str(repo_root.resolve())
    if expected_root in cache_text:
        return False
    shutil.rmtree(build_root)
    return True


def ensure_benchmark_runner(repo_root: Path, force_rebuild: bool = False) -> Path:
    runner_path = benchmark_runner_path(repo_root)
    sanitized = sanitize_release_build_dir(repo_root)
    if runner_path.exists() and not force_rebuild and not sanitized:
        return runner_path

    build_environment = duckdb_build_environment()
    if not bool(build_environment["can_rebuild"]):
        raise RuntimeError(
            "DuckDB benchmark runner build requested but no usable cmake was found. "
            "Set CPP_PERF_DUCKDB_CMAKE_BIN or make cmake available on PATH."
        )

    if has_valid_release_build_config(repo_root):
        subprocess.run(
            [
                str(build_environment["cmake_path"]),
                "--build",
                ".",
                "--config",
                "Release",
                "--target",
                "benchmark_runner",
            ],
            cwd=release_build_root(repo_root),
            check=True,
            text=True,
            env=build_environment["env"],
        )
    else:
        command = list(BUILD_COMMAND)
        extra_args = os.environ.get("CPP_PERF_DUCKDB_BUILD_ARGS", "").strip()
        if extra_args:
            command.extend(shlex.split(extra_args))
        subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            text=True,
            env=build_environment["env"],
        )
    if not runner_path.exists():
        raise FileNotFoundError(f"DuckDB benchmark runner not found after build: {runner_path}")
    return runner_path


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        raise ValueError("Cannot compute percentile of empty sample")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * ratio) - 1)
    return ordered[min(index, len(ordered) - 1)]


def parse_timings_file(timings_path: Path) -> dict[str, object]:
    samples = [float(line.strip()) for line in timings_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not samples:
        raise ValueError(f"No timings found in {timings_path}")

    median_seconds = statistics.median(samples)
    mean_seconds = statistics.fmean(samples)
    deviation_seconds = statistics.pstdev(samples) if len(samples) > 1 else 0.0
    p99_seconds = percentile(samples, 0.99)
    cv = 0.0 if mean_seconds == 0.0 else deviation_seconds / mean_seconds
    stats = {
        "median": median_seconds * 1_000_000_000.0,
        "p99": p99_seconds * 1_000_000_000.0,
        "stable": cv <= 0.05,
        "sample_count": len(samples),
        "coefficient_of_variation": cv,
        "unit": "ns",
    }
    return {
        "median_ns": stats["median"],
        "p99_ns": stats["p99"],
        "stable": stats["stable"],
        "correctness": True,
        "stats": stats,
    }


def run_duckdb_benchmark(
    repo_root: Path,
    benchmark_path: str,
    out_path: Path,
    label: str,
    force_rebuild: bool = False,
) -> dict[str, object]:
    runner = ensure_benchmark_runner(repo_root, force_rebuild=force_rebuild)
    command = [str(runner), benchmark_path, f"--out={out_path}"]
    extra_args = os.environ.get("CPP_PERF_DUCKDB_BENCHMARK_ARGS", "").strip()
    if extra_args:
        command.extend(shlex.split(extra_args))

    process = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    out_path.with_suffix(".stdout").write_text(process.stdout, encoding="utf-8")
    out_path.with_suffix(".stderr").write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError(
            f"DuckDB benchmark runner failed for {benchmark_path} with exit code {process.returncode}"
        )
    if not out_path.exists():
        raise FileNotFoundError(f"Benchmark timings file was not created: {out_path}")

    payload = parse_timings_file(out_path)
    payload["benchmark_path"] = benchmark_path
    payload["runner_path"] = str(runner)
    payload["label"] = label
    return payload


def load_manifest(case_dir: Path) -> dict[str, object]:
    return load_json(case_dir / "manifest.json")


def normalize_optimize_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    normalized["changed"] = normalize_bool(payload.get("changed"), default=False)
    normalized["rebuild"] = normalize_bool(payload.get("rebuild"), default=False)
    normalized["correctness"] = normalize_bool(payload.get("correctness"), default=True)
    terminal_state = payload.get("terminal_state")
    if isinstance(terminal_state, str) and terminal_state.strip():
        normalized["terminal_state"] = terminal_state.strip().lower().replace("-", "_")
    notes = payload.get("notes")
    normalized["notes"] = "" if notes is None else str(notes)
    return normalized
