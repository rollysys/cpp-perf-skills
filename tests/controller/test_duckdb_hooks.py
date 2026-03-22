from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.cpp_perf_campaign.hooks.duckdb_common import (
    DEFAULT_BENCHMARK,
    build_capability_payload,
    ensure_benchmark_runner,
    parse_timings_file,
    sanitize_release_build_dir,
    select_benchmark_for_target,
    suggest_benchmark_for_target,
)
from tools.cpp_perf_campaign.hooks.duckdb_optimize import (
    _build_claude_command,
    _build_claude_prompt,
    _extract_claude_payload,
    _infer_terminal_state,
    _normalize_claude_payload,
)


class DuckDBHookTest(unittest.TestCase):
    def test_suggest_benchmark_for_known_areas(self) -> None:
        selection = suggest_benchmark_for_target("src/execution/expression_executor.cpp")
        self.assertEqual(selection.benchmark_path, "benchmark/micro/aggregate/simple_group.benchmark")
        self.assertEqual(selection.reason, "execution")

        parquet_selection = suggest_benchmark_for_target("extension/parquet/parquet_reader.cpp")
        self.assertEqual(
            parquet_selection.benchmark_path,
            "benchmark/parquet/dictionary_read-short-1000000.benchmark",
        )
        self.assertEqual(parquet_selection.reason, "parquet")

    def test_select_benchmark_falls_back_when_selected_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir)
            (repo_root / "benchmark" / "micro" / "nulls").mkdir(parents=True)
            (repo_root / DEFAULT_BENCHMARK).write_text("stub\n", encoding="utf-8")
            selection = select_benchmark_for_target(repo_root, "src/function/cast.cpp")
            self.assertEqual(selection.benchmark_path, DEFAULT_BENCHMARK)
            self.assertEqual(selection.reason, "function_fallback")

    def test_parse_timings_file_returns_ns_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            timings_path = Path(tempdir) / "timings.out"
            timings_path.write_text("0.100\n0.101\n0.099\n0.100\n0.102\n", encoding="utf-8")
            payload = parse_timings_file(timings_path)

        stats = payload["stats"]
        assert isinstance(stats, dict)
        self.assertAlmostEqual(float(stats["median"]), 100_000_000.0)
        self.assertAlmostEqual(float(stats["p99"]), 102_000_000.0)
        self.assertTrue(bool(stats["stable"]))
        self.assertEqual(int(stats["sample_count"]), 5)

    def test_extract_claude_payload_reads_structured_output(self) -> None:
        payload = _extract_claude_payload(
            '{"type":"result","structured_output":{"changed":true,"rebuild":true,"correctness":true,"files_touched":["src/foo.cpp"],"summary":"done","notes":"ok"}}'
        )
        self.assertTrue(bool(payload["changed"]))
        self.assertEqual(payload["files_touched"], ["src/foo.cpp"])

    def test_normalize_claude_payload_infers_rebuild_from_source_files(self) -> None:
        payload = _normalize_claude_payload(
            {
                "changed": True,
                "rebuild": False,
                "correctness": True,
                "files_touched": ["src/execution/adaptive_filter.cpp"],
                "summary": "Applied a small loop simplification.",
                "notes": "",
                "terminal_state": "hardware_limit",
            },
            strategy="vectorize",
        )
        self.assertTrue(bool(payload["rebuild"]))
        self.assertEqual(payload["strategy"], "vectorize")
        self.assertEqual(payload["notes"], "Applied a small loop simplification.")
        self.assertEqual(payload["terminal_state"], "hardware_limit")

    def test_build_claude_prompt_mentions_skill_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb"
            case_dir = repo_root / ".cpp-perf" / "campaigns" / "duckdb-smoke" / "cases" / "000001"
            controller_root = Path("/Users/x/cpp_perf_skills")
            case_dir.mkdir(parents=True)
            manifest = {
                "target_path": str(repo_root / "src" / "execution" / "adaptive_filter.cpp"),
                "benchmark_path": "benchmark/micro/aggregate/simple_group.benchmark",
                "build_capability": {"can_rebuild": False, "cmake_path": None},
            }
            prompt = _build_claude_prompt(
                repo_root=repo_root,
                case_dir=case_dir,
                strategy="vectorize",
                manifest=manifest,
                controller_root=controller_root,
            )
        self.assertIn("skills/cpp-perf/SKILL.md", prompt)
        self.assertIn("adaptive_filter.cpp", prompt)
        self.assertIn("vectorize", prompt)
        self.assertIn("Rebuilds are currently unavailable", prompt)

    def test_build_claude_command_uses_clean_mode_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb"
            case_dir = Path(tempdir) / "case"
            controller_root = Path("/Users/x/cpp_perf_skills")
            repo_root.mkdir()
            case_dir.mkdir()
            command = _build_claude_command(
                claude_bin="claude",
                repo_root=repo_root,
                controller_root=controller_root,
                case_dir=case_dir,
                prompt="Optimize this file.",
            )
            mcp_config_path = case_dir / "claude_mcp_config.json"
            self.assertIn("--no-chrome", command)
            self.assertIn("--strict-mcp-config", command)
            self.assertIn("--mcp-config", command)
            self.assertIn(str(mcp_config_path), command)
            self.assertEqual(mcp_config_path.read_text(encoding="utf-8").strip(), '{"mcpServers":{}}')
            self.assertIn("--setting-sources", command)
            self.assertIn("project,local", command)
            self.assertEqual(command[-2], "--")
            self.assertEqual(command[-1], "Optimize this file.")

    def test_build_capability_payload_uses_explicit_cmake(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb"
            repo_root.mkdir()
            cmake_bin = Path(tempdir) / "bin" / "cmake"
            cmake_bin.parent.mkdir()
            cmake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            cmake_bin.chmod(0o755)
            with patch.dict(os.environ, {"CPP_PERF_DUCKDB_CMAKE_BIN": str(cmake_bin)}, clear=False):
                payload = build_capability_payload(repo_root)
        self.assertTrue(bool(payload["can_rebuild"]))
        self.assertEqual(payload["cmake_path"], str(cmake_bin.resolve()))

    def test_sanitize_release_build_dir_removes_foreign_cmake_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb-clean"
            build_root = repo_root / "build" / "release"
            build_root.mkdir(parents=True)
            (build_root / "CMakeCache.txt").write_text(
                "CMAKE_HOME_DIRECTORY:INTERNAL=/tmp/duckdb-cpp-perf\n",
                encoding="utf-8",
            )
            removed = sanitize_release_build_dir(repo_root)
        self.assertTrue(removed)
        self.assertFalse(build_root.exists())

    def test_ensure_benchmark_runner_rebuilds_when_existing_runner_comes_from_foreign_build(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb-clean"
            runner_path = repo_root / "build" / "release" / "benchmark" / "benchmark_runner"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text("old-runner\n", encoding="utf-8")
            (repo_root / "build" / "release" / "CMakeCache.txt").write_text(
                "CMAKE_HOME_DIRECTORY:INTERNAL=/tmp/duckdb-cpp-perf\n",
                encoding="utf-8",
            )
            cmake_bin = Path(tempdir) / "bin" / "cmake"
            cmake_bin.parent.mkdir()
            cmake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            cmake_bin.chmod(0o755)

            def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                runner_path.parent.mkdir(parents=True, exist_ok=True)
                runner_path.write_text("new-runner\n", encoding="utf-8")
                return subprocess.CompletedProcess(args=args[0], returncode=0)

            with patch.dict(os.environ, {"CPP_PERF_DUCKDB_CMAKE_BIN": str(cmake_bin)}, clear=False):
                with patch("tools.cpp_perf_campaign.hooks.duckdb_common.subprocess.run", side_effect=fake_run) as mock_run:
                    resolved = ensure_benchmark_runner(repo_root, force_rebuild=False)
                    self.assertEqual(resolved, runner_path)
                    self.assertEqual(mock_run.call_count, 1)
                    self.assertEqual(runner_path.read_text(encoding="utf-8"), "new-runner\n")

    def test_ensure_benchmark_runner_uses_targeted_rebuild_for_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repo_root = Path(tempdir) / "duckdb-clean"
            runner_path = repo_root / "build" / "release" / "benchmark" / "benchmark_runner"
            runner_path.parent.mkdir(parents=True)
            runner_path.write_text("runner\n", encoding="utf-8")
            (repo_root / "build" / "release" / "CMakeCache.txt").write_text(
                f"CMAKE_HOME_DIRECTORY:INTERNAL={repo_root.resolve()}\n",
                encoding="utf-8",
            )
            cmake_bin = Path(tempdir) / "bin" / "cmake"
            cmake_bin.parent.mkdir()
            cmake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            cmake_bin.chmod(0o755)

            with patch.dict(os.environ, {"CPP_PERF_DUCKDB_CMAKE_BIN": str(cmake_bin)}, clear=False):
                with patch("tools.cpp_perf_campaign.hooks.duckdb_common.subprocess.run") as mock_run:
                    resolved = ensure_benchmark_runner(repo_root, force_rebuild=True)

            self.assertEqual(resolved, runner_path)
            self.assertEqual(mock_run.call_count, 1)
            command = mock_run.call_args.args[0]
            self.assertEqual(
                command,
                [str(cmake_bin.resolve()), "--build", ".", "--config", "Release", "--target", "benchmark_runner"],
            )

    def test_infer_terminal_state_marks_no_more_ideas_after_repeated_dead_ends(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            case_dir = Path(tempdir) / "case"
            case_dir.mkdir()
            target_memory_path = case_dir / "target_memory.json"
            target_memory_path.write_text(
                json.dumps(
                    {
                        "recent_experiments": [
                            {"changed": False, "outcome": "discard", "notes": "no safe win"},
                            {"changed": False, "outcome": "discard", "notes": "same dead end"},
                        ],
                        "strategy_status": [
                            {"name": "vectorize", "attempts": 1, "max_passes": 1, "last_outcome": "discard"},
                            {"name": "layout", "attempts": 1, "max_passes": 2, "last_outcome": "discard"},
                        ],
                        "guidance": {"successful_directions": []},
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "changed": False,
                "rebuild": False,
                "correctness": True,
                "files_touched": [],
                "summary": "No additional optimization found.",
                "notes": "No additional optimization found.",
                "terminal_state": None,
            }
            with patch.dict(
                os.environ,
                {
                    "CPP_PERF_TARGET_MEMORY_PATH": str(target_memory_path),
                    "CPP_PERF_STRATEGY_PASS": "2",
                    "CPP_PERF_NO_MORE_IDEAS_STREAK": "3",
                },
                clear=False,
            ):
                normalized = _infer_terminal_state(payload, case_dir, "layout")
        self.assertEqual(normalized["terminal_state"], "no_more_ideas")

    def test_infer_terminal_state_marks_hardware_limit_from_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            case_dir = Path(tempdir) / "case"
            case_dir.mkdir()
            target_memory_path = case_dir / "target_memory.json"
            target_memory_path.write_text(
                json.dumps(
                    {
                        "recent_experiments": [
                            {"changed": True, "outcome": "keep", "notes": "kept SIMD rewrite"},
                            {"changed": False, "outcome": "discard", "notes": "no further gain"},
                        ],
                        "strategy_status": [
                            {"name": "vectorize", "attempts": 1, "max_passes": 1, "last_outcome": "keep"},
                            {"name": "layout", "attempts": 1, "max_passes": 2, "last_outcome": "discard"},
                        ],
                        "guidance": {
                            "successful_directions": [
                                {"strategy": "vectorize", "speedup": 1.18, "summary": "SIMD cleanup"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "changed": False,
                "rebuild": False,
                "correctness": True,
                "files_touched": [],
                "summary": "Loop is already vectorized and memory-bound.",
                "notes": "The benchmark looks bandwidth-bound now.",
                "terminal_state": None,
            }
            with patch.dict(
                os.environ,
                {
                    "CPP_PERF_TARGET_MEMORY_PATH": str(target_memory_path),
                    "CPP_PERF_STRATEGY_PASS": "2",
                },
                clear=False,
            ):
                normalized = _infer_terminal_state(payload, case_dir, "layout")
        self.assertEqual(normalized["terminal_state"], "hardware_limit")

    def test_infer_terminal_state_rejects_early_no_more_ideas_when_other_strategies_remain(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            case_dir = Path(tempdir) / "case"
            case_dir.mkdir()
            target_memory_path = case_dir / "target_memory.json"
            target_memory_path.write_text(
                json.dumps(
                    {
                        "recent_experiments": [],
                        "strategy_status": [
                            {"name": "vectorize", "attempts": 0, "max_passes": 1, "last_outcome": None},
                            {"name": "branch", "attempts": 0, "max_passes": 1, "last_outcome": None},
                            {"name": "layout", "attempts": 0, "max_passes": 2, "last_outcome": None},
                        ],
                        "guidance": {"successful_directions": []},
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "changed": False,
                "rebuild": False,
                "correctness": True,
                "files_touched": [],
                "summary": "Not hot enough for vectorization.",
                "notes": "No vector opportunities found.",
                "terminal_state": "no_more_ideas",
            }
            with patch.dict(
                os.environ,
                {
                    "CPP_PERF_TARGET_MEMORY_PATH": str(target_memory_path),
                    "CPP_PERF_STRATEGY_PASS": "1",
                },
                clear=False,
            ):
                normalized = _infer_terminal_state(payload, case_dir, "vectorize")
        self.assertIsNone(normalized["terminal_state"])


if __name__ == "__main__":
    unittest.main()
